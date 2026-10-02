"""Caption-pack generation engine (v1).

Deterministic template + variant engine seeded with our proven caption
corpus. No LLM, no network calls, sub-100ms per pack.

- cybersecurity niche  -> served from CYBER_PACK (distilled from the
  published 30-pack Jeshua approved on 2026-09-30)
- everything else       -> generic template engine with tone control
"""

from __future__ import annotations

import hashlib
import random
import re
import uuid

PLATFORM_RULES = {
    "instagram": {"max_chars": 2200, "max_hashtags": 8},
    "linkedin": {"max_chars": 3000, "max_hashtags": 5},
    "x": {"max_chars": 280, "max_hashtags": 3},
    "tiktok": {"max_chars": 2200, "max_hashtags": 5},
}

TONES = ("warm", "bold", "professional", "playful")

CYBER_KEYWORDS = (
    "cyber", "security", "phishing", "hacker", "malware", "ransomware",
    "password", "mfa", "infosec", "breach", "vpn", "scam",
)

# ---------------------------------------------------------------------------
# Proven corpus: distilled from the published 30-pack (hook / body / closer /
# hashtags). Each entry: t=subtopic, h=hook, b=body, c=closer, tags=hashtags.
# ---------------------------------------------------------------------------
CYBER_PACK = [
    dict(t="phishing", h="Your inbox is the new front line. \U0001f3a3",
         b="Most breaches don't start with genius hackers \u2014 they start with a friendly-looking email asking you to \u201cverify your account real quick.\u201d Trust nothing, verify everything: hover the link, check the sender's real address.",
         c="What's the wildest phishing email you've ever caught? \U0001f447",
         tags=["#CyberSecurity", "#PhishingAwareness", "#InfoSec", "#StayCyberSafe"]),
    dict(t="mfa", h="That 6-digit code is annoying. You know what's more annoying? Explaining a breach. \U0001f510",
         b="Multi-factor authentication blocks the vast majority of account takeovers \u2014 and it takes four seconds. If any of your accounts still don't have MFA on, consider this your sign.",
         c="Which app's MFA setup gave you the most grief? \U0001f605",
         tags=["#MFA", "#CyberSecurity", "#InfoSec", "#TechTips"]),
    dict(t="password reuse", h="Using the same password everywhere is like having one key for your house, your car, and your safe \u2014 then handing out copies. \U0001f511",
         b="One breach and every account you own is an open door. A password manager fixes this in an afternoon: move your logins in and let it generate the ugly 20-character monsters.",
         c="Be honest \u2014 how many accounts still share one password? \U0001f447",
         tags=["#PasswordSecurity", "#CyberSecurity", "#InfoSec"]),
    dict(t="passphrases", h="\u201cTr0ub4dor&3\u201d is hard to remember and easy to crack. \u201ccorrect horse battery staple\u201d is the opposite. \U0001f434",
         b="Four random words beat clever character swaps every time. Length is what matters \u2014 make it long, make it weird, make it yours.",
         c="What's your trick for remembering strong passwords?",
         tags=["#PasswordSecurity", "#CyberSecurity", "#InfoSec"]),
    dict(t="software updates", h="That \u201cupdate available\u201d notification isn't a suggestion. It's a shield. \U0001f6e1\ufe0f",
         b="Updates patch the exact holes attackers are already using. Every day you hit \u201cremind me later,\u201d you're volunteering to be the easy target.",
         c="Update-immediately person or remind-me-later rebel? \U0001f62c",
         tags=["#CyberSecurity", "#PatchManagement", "#InfoSec"]),
    dict(t="public wi-fi", h="Free airport Wi-Fi comes with a hidden surcharge: everyone else on it. \u2615",
         b="On open networks your traffic is basically postcards \u2014 readable by anyone nosy enough to look. A VPN seals the envelope. No VPN? Avoid banking and logins until you're home.",
         c="Ever done something on public Wi-Fi you probably shouldn't have? \U0001f440",
         tags=["#CyberSecurity", "#PublicWiFi", "#InfoSec"]),
    dict(t="backups vs ransomware", h="Ransomware has one weakness: a backup it can't touch. \U0001f4be",
         b="3-2-1 rule: 3 copies, 2 different media, 1 offsite. And test the restore \u2014 a backup you've never restored is a rumor, not a plan.",
         c="When's the last time you actually backed up? Be honest. \U0001f447",
         tags=["#Ransomware", "#CyberSecurity", "#InfoSec"]),
    dict(t="phone social engineering", h="\u201cHi, this is IT, I need your password.\u201d No. Just no. \U0001f4de",
         b="Real IT will never ask for your password. Attackers love the phone because urgency short-circuits thinking \u2014 slow down, hang up, call the official number back.",
         c="Has anyone ever tried the \u201cthis is IT\u201d trick on you?",
         tags=["#SocialEngineering", "#CyberSecurity", "#InfoSec"]),
    dict(t="tailgating", h="The most effective hacking tool ever made? Holding the door with a smile. \U0001f6aa",
         b="Tailgating \u2014 following someone through a secured door \u2014 bypasses every badge reader in the building. It's not rude to ask for a badge. It's your job.",
         c="Polite or secure: which wins at your office? \U0001f447",
         tags=["#PhysicalSecurity", "#CyberSecurity", "#InfoSec"]),
    dict(t="usb drops", h="Found a USB stick in the parking lot? Congratulations, it's a trap. \U0001f4bd",
         b="Dropped drives are one of the oldest tricks in the book \u2014 curiosity does the attacker's work for them. Found media goes to security, never into your laptop.",
         c="Would you plug in a USB you found on the ground? (Say no. \U0001f605)",
         tags=["#CyberSecurity", "#InfoSec", "#TechTips"]),
    dict(t="password managers", h="Your brain was not designed to remember 200 passwords. Stop asking it to. \U0001f9e0",
         b="A password manager remembers them, generates strong ones, and warns you about breaches. The single highest-ROI security habit for normal humans.",
         c="Which password manager do you trust \u2014 or full memory-mode? \U0001f447",
         tags=["#PasswordManager", "#CyberSecurity", "#InfoSec"]),
    dict(t="2am incident", h="Nobody's security plan survives 2 AM. That's when you find out if your docs were real. \U0001f319",
         b="The teams that recover fast aren't the smartest \u2014 they're the ones who wrote the runbook before the fire and actually tested the restore.",
         c="When did you last test YOUR recovery plan \u2014 not just write it?",
         tags=["#IncidentResponse", "#CyberSecurity", "#InfoSec"]),
    dict(t="security culture", h="The security team can't protect what the whole company keeps clicking. \U0001f91d",
         b="Every employee is either a sensor or a vulnerability \u2014 no third option. The orgs that get this right have better culture, not better tools.",
         c="What's one thing your workplace does well on security? \U0001f447",
         tags=["#SecurityCulture", "#CyberSecurity", "#InfoSec"]),
    dict(t="patch procrastination", h="Hackers read patch notes too. Every update you delay is a map you're handing them. \U0001f5fa\ufe0f",
         b="The window between patch release and mass exploitation keeps shrinking. Same-day patching went from \u201cnice\u201d to \u201cnecessary\u201d a while back.",
         c="What finally got you to stop hitting \u201cremind me tomorrow\u201d?",
         tags=["#PatchManagement", "#CyberSecurity", "#InfoSec"]),
    dict(t="sim swapping", h="Your phone number is a master key \u2014 and attackers know it. \U0001f4f1",
         b="SIM swapping hijacks your number and intercepts MFA codes. Defense: a PIN on your carrier account, and app-based MFA instead of SMS wherever you can.",
         c="Did you know your carrier account needs its own PIN? Most people don't. \U0001f447",
         tags=["#SIMSwapping", "#MFA", "#CyberSecurity"]),
    dict(t="qr code scams", h="That QR code on the parking meter might not take you where you think. \U0001f4f7",
         b="Quishing \u2014 QR phishing \u2014 is rising fast because nobody inspects a QR code before scanning. Preview the URL first. If your camera won't show it, that's your answer.",
         c="Ever scanned a public QR code without thinking twice?",
         tags=["#Phishing", "#CyberSecurity", "#InfoSec"]),
    dict(t="home router", h="Your home router is the front door of your digital house \u2014 and most people never changed the lock. \U0001f3e0",
         b="Default admin passwords are public knowledge. Log in, change it, turn on WPA3, update the firmware. Twenty minutes, once, and you're done.",
         c="When did you last touch your router settings? (No judgment. Okay, a little. \U0001f604)",
         tags=["#HomeNetwork", "#CyberSecurity", "#InfoSec"]),
    dict(t="shoulder surfing", h="The person behind you in the coffee shop just watched you type your password. Probably. \U0001f440",
         b="Shoulder surfing is low-tech and everywhere. Privacy screen, situational awareness, and never typing credentials where strangers can watch.",
         c="Sketchiest place you've ever logged into something sensitive?",
         tags=["#CyberSecurity", "#InfoSec", "#PrivacyMatters"]),
    dict(t="old accounts", h="You have accounts you haven't touched in five years. Attackers love those. \U0001f47b",
         b="Dormant accounts with old passwords and no MFA are easy pickings \u2014 and they're tied to your email. Once a year: hunt them down, delete or secure them.",
         c="What's the oldest account you remember making? Mine's embarrassing. \U0001f605",
         tags=["#DigitalHygiene", "#CyberSecurity", "#InfoSec"]),
    dict(t="wrong-number texts", h="\u201cHi, is this Sarah?\u201d No. No it isn't. And now the scam begins. \U0001f4ac",
         b="Wrong-number texts open pig-butchering scams \u2014 friendly stranger, slow build, then the crypto \u201cinvestment opportunity.\u201d Delete, block, move on.",
         c="Gotten one of these lately? They're everywhere right now. \U0001f447",
         tags=["#ScamAwareness", "#CyberSecurity", "#InfoSec"]),
    dict(t="linkedin impersonation", h="That recruiter with 3 connections and a stock photo? Not a recruiter. \U0001f4bc",
         b="Fake profiles harvest info and launch targeted phishing. New account, thin network, generic posts \u2014 verify through the company site before you engage.",
         c="Ever been approached by an obvious fake? What gave them away? \U0001f447",
         tags=["#SocialEngineering", "#CyberSecurity", "#InfoSec"]),
    dict(t="link inspection", h="Before you click: hover. Two seconds. That's the whole technique. \U0001f5b1\ufe0f",
         b="Hovering reveals the real destination \u2014 \u201cmicorsoft-support.net\u201d is not Microsoft. On mobile, long-press to preview. Attackers count on you not bothering. Bother.",
         c="Teach one person this trick today. Who's it gonna be? \U0001f447",
         tags=["#Phishing", "#CyberSecurity", "#InfoSec"]),
    dict(t="incident response plan", h="\u201cWhat's our incident response plan?\u201d should never be asked DURING the incident. \U0001f6a8",
         b="Write it calm, test it calm, so you can run it in chaos. Who calls whom, who can pull the plug, where backups live. One page beats a binder nobody opens.",
         c="Does your team have a plan \u2014 or a hope? \U0001f62c",
         tags=["#IncidentResponse", "#CyberSecurity", "#InfoSec"]),
    dict(t="least privilege", h="Nobody needs admin access \u201cjust in case.\u201d They need it for the task, then it goes back. \U0001f512",
         b="Least privilege isn't about trust \u2014 it's about blast radius. When an account gets compromised, limited permissions limit the damage.",
         c="When did you last audit who has access to what? \U0001f447",
         tags=["#ZeroTrust", "#CyberSecurity", "#InfoSec"]),
    dict(t="security training", h="Nobody remembers slide 47 of the compliance deck. Everybody remembers the phishing test that got them. \U0001f3a3",
         b="Training that works is short, real, and repeated. Five minutes a month beats five hours a year. Make it stories, not slides.",
         c="What security training actually stuck with YOU?",
         tags=["#SecurityAwareness", "#CyberSecurity", "#InfoSec"]),
    dict(t="holiday scams", h="Scammers love the holidays almost as much as you do. \U0001f384",
         b="Fake delivery texts, too-good-to-be-true deals, charity scams \u2014 volume spikes every season because distracted shoppers click faster. Verify the sender; never shop through a text link.",
         c="What's the sketchiest \u201cdeal\u201d text you've gotten? \U0001f447",
         tags=["#ScamAwareness", "#CyberSecurity", "#InfoSec"]),
    dict(t="ai voice scams", h="They can clone your kid's voice from a 10-second clip now. Let that sink in. \U0001f399\ufe0f",
         b="AI voice scams target grandparents with scripts that sound exactly right. Family defense: a secret code word. No code word \u2014 hang up and call back a known number.",
         c="Does your family have a code word? If not, today's the day. \U0001f447",
         tags=["#AIScams", "#CyberSecurity", "#InfoSec"]),
    dict(t="browser extensions", h="That free browser extension can read everything you type. Everything. \U0001f9e9",
         b="Extensions are tiny apps with giant permissions. Don't use it weekly? Remove it. Never heard of the developer? Remove it faster.",
         c="How many extensions are in your browser right now? Go count. I'll wait. \U0001f604",
         tags=["#BrowserSecurity", "#CyberSecurity", "#InfoSec"]),
    dict(t="it won't happen to me", h="\u201cIt won't happen to me\u201d is the most expensive sentence in cybersecurity. \U0001f4b8",
         b="Attackers don't care about your profile, they care about your passwords. MFA, updates, backups \u2014 basic hygiene defeats the vast majority of attacks.",
         c="What finally made security \u201creal\u201d for you? \U0001f447",
         tags=["#CyberSecurity", "#InfoSec", "#SecurityAwareness"]),
    dict(t="human firewall", h="Tools help. But the strongest firewall you'll ever own is an informed human. \U0001f9e0",
         b="Technology fails, processes get skipped \u2014 but a person who pauses before clicking saves the day more often than any appliance.",
         c="Tag someone who's YOUR human firewall. They deserve the credit. \U0001f447",
         tags=["#CyberSecurity", "#InfoSec", "#SecurityCulture"]),
]

# ---------------------------------------------------------------------------
# Generic template engine (any other topic)
# ---------------------------------------------------------------------------
HOOK_TEMPLATES = [
    "{Topic}: the thing nobody tells {audience}.",
    "Stop doing {topic} the hard way.",
    "What if {topic} took five minutes instead of five hours?",
    "{Audience}, this one's for you \u2014 {topic}, demystified.",
    "The {topic} mistake almost everyone makes.",
    "Nobody talks about this side of {topic}.",
    "Read this before your next {topic} attempt.",
    "{Topic} isn't complicated. It's just poorly explained.",
]

BODY_TEMPLATES = [
    "Here's the short version: start small, stay consistent, and ignore the gurus selling shortcuts. {Audience} get the best results from fundamentals done well \u2014 not hacks.",
    "Most people overcomplicate {topic}. Pick one thing to improve this week, measure it, and repeat. Boring? Yes. Effective? Every time.",
    "After years of watching people tackle {topic}, the pattern is clear: the ones who win aren't the most talented \u2014 they're the most consistent.",
    "You don't need another course on {topic}. You need a simple checklist and the discipline to run it. Start today, thank yourself in 90 days.",
    "The secret to {topic} isn't a secret at all: show up, do the reps, and don't quit in week three like everyone else.",
    "Forget perfect. {Audience} who ship imperfect {topic} work weekly beat perfectionists every single time.",
]

CTA_BANK = {
    "warm": [
        "What's your biggest {topic} challenge right now? Tell me below \u2014 I read everything. \U0001f447",
        "Which of these are you trying first? Drop a comment and let's talk it through. \U0001f447",
    ],
    "bold": [
        "Pick ONE {topic} action and do it today. Report back. No excuses. \U0001f447",
        "Stop scrolling, start doing. Your first {topic} move happens in the next 10 minutes. \U0001f447",
    ],
    "professional": [
        "How does your team approach {topic}? Share what's working. \U0001f447",
        "What's one {topic} lesson you'd pass to someone starting out?",
    ],
    "playful": [
        "Be honest \u2014 are you nailing {topic} or winging it? \U0001f605\U0001f447",
        "Tag someone who needs this {topic} pep talk today. \U0001f447",
    ],
}

_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001f9ff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u200d]+", flags=re.UNICODE
)


def _apply_tone(text: str, tone: str) -> str:
    # v1: 'professional' strips emojis; other tones keep the house voice.
    if tone == "professional":
        text = _EMOJI_RE.sub("", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def _seeded_rng(*parts: str) -> random.Random:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return random.Random(int(digest, 16))


def _tags_for_topic(topic: str, platform: str) -> list[str]:
    words = [w for w in re.findall(r"[A-Za-z]+", topic) if len(w) > 2]
    tags: list[str] = []
    if words:
        tags.append("#" + "".join(w.capitalize() for w in words[:3]))
        for w in words[:3]:
            tags.append("#" + w.capitalize())
    tags += ["#Tips", "#HowTo"]
    return tags[: PLATFORM_RULES[platform]["max_hashtags"]]


def _is_cyber(topic: str, niche: str | None) -> bool:
    if (niche or "").strip().lower() == "cybersecurity":
        return True
    lowered = topic.lower()
    return any(k in lowered for k in CYBER_KEYWORDS)


def _cyber_caption(entry: dict, tone: str, platform: str, include_hashtags: bool) -> dict:
    hook = _apply_tone(entry["h"], tone)
    body = _apply_tone(entry["b"], tone)
    cta = _apply_tone(entry["c"], tone)
    tags = entry["tags"][: PLATFORM_RULES[platform]["max_hashtags"]] if include_hashtags else []
    text = hook + "\n\n" + body + "\n\n" + cta
    if tags:
        text += "\n\n" + " ".join(tags)
    max_chars = PLATFORM_RULES[platform]["max_chars"]
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "\u2026"
    return {"hook": hook, "body": body, "cta": cta,
            "hashtags": tags, "char_count": len(text)}


def _generic_caption(topic: str, audience: str, tone: str, platform: str,
                     include_hashtags: bool, rng: random.Random) -> dict:
    topic_cap = topic[:1].upper() + topic[1:]
    aud_cap = audience[:1].upper() + audience[1:]
    hook = rng.choice(HOOK_TEMPLATES).format(topic=topic, Topic=topic_cap,
                                             audience=audience, Audience=aud_cap)
    body = rng.choice(BODY_TEMPLATES).format(topic=topic, Topic=topic_cap,
                                             audience=audience, Audience=aud_cap)
    cta = rng.choice(CTA_BANK[tone]).format(topic=topic)
    hook, body, cta = (_apply_tone(t, tone) for t in (hook, body, cta))
    tags = _tags_for_topic(topic, platform) if include_hashtags else []
    text = hook + "\n\n" + body + "\n\n" + cta
    if tags:
        text += "\n\n" + " ".join(tags)
    max_chars = PLATFORM_RULES[platform]["max_chars"]
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "\u2026"
    return {"hook": hook, "body": body, "cta": cta,
            "hashtags": tags, "char_count": len(text)}


def generate_pack(topic: str, audience: str, tone: str = "warm",
                  platform: str = "instagram", count: int = 5,
                  include_hashtags: bool = True,
                  niche: str | None = None) -> dict:
    """Build a caption pack. Raises ValueError on invalid input."""
    topic, audience = topic.strip(), audience.strip()
    if not topic or not audience:
        raise ValueError("topic and audience are required")
    if tone not in TONES:
        raise ValueError(f"tone must be one of {', '.join(TONES)}")
    if platform not in PLATFORM_RULES:
        raise ValueError(f"platform must be one of {', '.join(PLATFORM_RULES)}")
    if not 1 <= count <= 10:
        raise ValueError("count must be between 1 and 10")

    rng = _seeded_rng(topic, audience, tone, platform, str(count))
    captions: list[dict] = []

    if _is_cyber(topic, niche):
        order = list(range(len(CYBER_PACK)))
        rng.shuffle(order)
        for i in range(count):
            entry = CYBER_PACK[order[i % len(order)]]
            cap = _cyber_caption(entry, tone, platform, include_hashtags)
            cap["id"] = i + 1
            captions.append(cap)
        source = "proven-corpus:cybersecurity-30-pack"
    else:
        for i in range(count):
            cap = _generic_caption(topic, audience, tone, platform,
                                   include_hashtags, rng)
            cap["id"] = i + 1
            captions.append(cap)
        source = "template-engine:v1"

    return {
        "pack_id": "pack_" + uuid.uuid4().hex[:8],
        "topic": topic,
        "audience": audience,
        "tone": tone,
        "platform": platform,
        "source": source,
        "captions": captions,
    }

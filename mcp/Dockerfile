FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_server.py .

# Smithery sets PORT=8081; Render sets its own PORT. With PORT set the
# server runs streamable HTTP; without it, stdio (Claude Desktop / Cursor).
EXPOSE 8081

CMD ["python", "mcp_server.py"]

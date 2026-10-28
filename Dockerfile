FROM python:3.12-slim
   WORKDIR /app
   RUN pip install --no-cache-dir "mcp>=2.3" "beautifulsoup4>=4.12" "pypdfium2>=4.30"
   COPY server.py .
   ENV HOST=0.0.0.0 PORT=8000
   EXPOSE 8000
   CMD ["python", "server.py", "--http"]

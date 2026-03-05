FROM python:3.12-slim

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chrome --with-deps && chmod -R 755 /ms-playwright
# Replace the crashpad handler with a no-op stub. In a headless container there
# is no crash database, so the real handler exits with "--database is required"
# and Chrome responds with SIGTRAP. The stub stays alive (tail -f /dev/null)
# which is all Chrome needs — it never actually sends crashes to it.
RUN printf '#!/bin/sh\nexec tail -f /dev/null\n' \
      > /opt/google/chrome/chrome_crashpad_handler \
    && chmod +x /opt/google/chrome/chrome_crashpad_handler

COPY . .

CMD ["python", "main.py"]

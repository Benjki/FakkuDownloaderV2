FROM python:3.12-slim

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# The k8s pod runs as uid 1000 which has no home directory in this image.
# Chrome needs a writable $HOME to create its crashpad database directory;
# without it, it invokes chrome_crashpad_handler without --database and crashes.
ENV HOME=/tmp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chrome --with-deps && chmod -R 755 /ms-playwright

COPY . .

CMD ["python", "main.py"]

FROM python:3.12-slim

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chrome --with-deps && chmod -R 755 /ms-playwright

COPY . .

CMD ["python", "main.py"]

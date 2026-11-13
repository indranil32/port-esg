FROM python:3.12-slim
WORKDIR /srv
# OCR runtime libraries (rapidocr needs OpenCV's GL/glib; tesseract is the fallback engine)
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY db/01_schema.sql db/02_schema_ext.sql ./db/
COPY db/migrations ./db/migrations
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

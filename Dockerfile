FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scale_reader.py .
CMD ["python", "-u", "scale_reader.py"]

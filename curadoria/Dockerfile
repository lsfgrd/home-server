FROM python:3.13-slim
RUN pip install --no-cache-dir flask==3.1.* mutagen==1.47.* gunicorn==23.*
WORKDIR /app
COPY app.py .
COPY static static
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "4", "--timeout", "120", "app:app"]

FROM python:3.12.16-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt
COPY copybot ./copybot
# baza i klucz szyfrujący trzymane na trwałym dysku (na Railway: Volume podpięty pod /data)
ENV DB_PATH=/data/copybot.db
RUN mkdir -p /data
CMD ["python", "-m", "copybot.main"]

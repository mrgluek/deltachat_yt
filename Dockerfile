FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y git ffmpeg curl unzip && rm -rf /var/lib/apt/lists/*
# Install Deno (recommended JS runtime for yt-dlp n-challenge solving)
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_INSTALL=/root/.deno
ENV PATH=$DENO_INSTALL/bin:$PATH
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DC_DB_DIR=/app/data
ENV DB_PATH=/app/data/ytbot.db
VOLUME /app/data
CMD ["python", "-u", "bot.py", "serve"]

FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# unprivileged user; compose can override the uid/gid to match the host's media folders
RUN useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin radio

WORKDIR /app
COPY radio.py prepare.py ./

ENV PYTHONUNBUFFERED=1
USER radio
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/status', timeout=4)"

ENTRYPOINT ["python3", "/app/radio.py"]
CMD ["/cache"]

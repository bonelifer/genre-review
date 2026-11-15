FROM python:3.13-slim
RUN pip install --no-cache-dir --disable-pip-version-check mutagen==1.48.1
WORKDIR /app
COPY *.py genres.txt genres-extra.txt LICENSE LICENSE-beets ./
ENV LIBRARY_DIR=/library STATE_DIR=/state PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/app/genre_review.py"]
CMD ["serve"]

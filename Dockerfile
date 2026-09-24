FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY build_db.py query.py serve.py command_center.py collection.py cig.py cig_profiles.py agencies.py contacts.py email_sender.py newsletter.py wire_template.py images.py recommend.py usage.py entitlements.py readonly_api.py ./
COPY static ./static
COPY reference ./reference
ENV NTD_DB=/data/ntd.duckdb
EXPOSE 8000
CMD ["uvicorn", "serve:app", "--host", "0.0.0.0", "--port", "8000"]

import os
import re
import sqlite3
from flask import Flask, jsonify, render_template, request
import pdfplumber
import requests

app = Flask(__name__)

# Your Free API Keys (Both Gemini AQ. key from your screenshot and Groq gsk_ key included)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()


def init_db():
  conn = sqlite3.connect("lumi_ai.db")
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS lumi_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_query TEXT,
            ai_response TEXT
        )
    """)
  conn.commit()
  conn.close()


init_db()

CURRENT_PDF_TEXT = ""


def sanitize_text_before_llm(text, user_query=""):
  if not text:
    return ""
  sanitized = text
  sanitized = re.sub(
      r"\b\d{4}\s\d{4}\s\d{4}\b", "<REDACTED_AADHAAR>", sanitized
  )

  q_lower = user_query.lower()
  if not any(
      word in q_lower
      for word in ["contact", "phone", "number", "mobile", "email", "mail"]
  ):
    sanitized = re.sub(r"\b[6-9]\d{9}\b", "<REDACTED_PHONE>", sanitized)
    sanitized = re.sub(
        r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+",
        "<REDACTED_EMAIL>",
        sanitized,
    )
  return sanitized


def get_ai_response(prompt):
  system_msg = (
      "You are Lumi-AI, an Enterprise Document Intelligence Assistant. "
      "Answer clearly and accurately based strictly on the provided document"
      " context."
  )

  # 1. Try Groq Free API first (Dynamic live model discovery - 100% Free & Instant)
  if GROQ_API_KEY:
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
      models_resp = requests.get(
          "https://api.groq.com/openai/v1/models", headers=headers, timeout=10
      )
      if models_resp.status_code == 200:
        all_models = [
            m["id"]
            for m in models_resp.json().get("data", [])
            if not any(
                skip in m["id"].lower()
                for skip in ["whisper", "guard", "tts", "vision", "orpheus"]
            )
        ]
        # Prioritize Llama 3 models if available
        all_models.sort(key=lambda x: (0 if "llama" in x.lower() else 1))

        for model_name in all_models:
          chat_resp = requests.post(
              "https://api.groq.com/openai/v1/chat/completions",
              headers=headers,
              json={
                  "model": model_name,
                  "messages": [
                      {"role": "system", "content": system_msg},
                      {"role": "user", "content": prompt},
                  ],
                  "temperature": 0.2,
                  "max_tokens": 1024,
              },
              timeout=25,
          )
          if chat_resp.status_code == 200:
            return chat_resp.json()["choices"][0]["message"]["content"]
    except Exception:
      pass

  # 2. Try Gemini API (Supports both AQ. Auth Keys and AIzaSy Keys via Header & Query Param)
  if GEMINI_API_KEY:
    for gemini_model in ["gemini-2.5-flash", "gemini-2.0-flash"]:
      url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent"
      payload = {
          "contents": [{"parts": [{"text": f"{system_msg}\n\n{prompt}"}]}]
      }

      # Try with x-goog-api-key header AND Bearer header for AQ. auth keys
      for headers in [
          {
              "x-goog-api-key": GEMINI_API_KEY,
              "Content-Type": "application/json",
          },
          {
              "Authorization": f"Bearer {GEMINI_API_KEY}",
              "Content-Type": "application/json",
          },
      ]:
        try:
          r = requests.post(url, headers=headers, json=payload, timeout=25)
          if r.status_code == 200:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
          pass

  return (
      "Error: Both API keys failed to respond. Please check your internet"
      " connection or click 'Copy cURL quickstart' on your AI Studio popup to"
      " verify key status."
  )


@app.route("/")
def home():
  return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload_pdf():
  global CURRENT_PDF_TEXT
  if "pdf" not in request.files:
    return jsonify({"error": "No file selected."}), 400

  file = request.files["pdf"]
  if file.filename == "":
    return jsonify({"error": "Empty filename."}), 400

  extracted_text = ""
  try:
    with pdfplumber.open(file) as pdf:
      for page in pdf.pages:
        text = page.extract_text()
        if text:
          extracted_text += text + "\n"
      page_count = len(pdf.pages)

    CURRENT_PDF_TEXT = extracted_text.strip()
    return jsonify({
        "message": (
            f"Success! Indexed {page_count} pages from {file.filename}."
        )
    })
  except Exception as e:
    return jsonify({"error": f"Error parsing PDF: {str(e)}"}), 500


@app.route("/ask", methods=["POST"])
def ask_lumi():
  data = request.get_json(silent=True) or {}
  user_query = data.get("question", "").strip()

  if not user_query:
    return jsonify({"error": "Query cannot be empty."}), 400

  global CURRENT_PDF_TEXT
  if not CURRENT_PDF_TEXT:
    return jsonify({"answer": "Please upload and index a PDF document first."})

  safe_context = sanitize_text_before_llm(CURRENT_PDF_TEXT, user_query)
  full_prompt = (
      f"Document Context:\n{safe_context}\n\nUser Question:\n{user_query}"
  )

  ai_response = get_ai_response(full_prompt)

  try:
    conn = sqlite3.connect("lumi_ai.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO lumi_history (user_query, ai_response) VALUES (?, ?)",
        (user_query, ai_response),
    )
    conn.commit()
    conn.close()
  except Exception as db_err:
    print(f"DB Error: {db_err}")

  return jsonify({"answer": ai_response})


if __name__ == "__main__":
  app.run(debug=True, port=5000)
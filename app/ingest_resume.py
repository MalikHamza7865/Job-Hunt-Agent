#!/usr/bin/env python3
"""Ingest a resume (PDF or plain text) into the Supabase `resume` table.

Usage:
    python ingest_resume.py path/to/resume.pdf
    python ingest_resume.py path/to/resume.txt
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pypdf import PdfReader

from app.db import get_client


COMMON_SKILLS = [
    "python", "django", "flask", "fastapi", "javascript", "typescript",
    "react", "node", "node.js", "sql", "postgres", "postgresql", "mysql",
    "mongodb", "redis", "docker", "kubernetes", "aws", "gcp", "azure",
    "terraform", "ci/cd", "jenkins", "github actions", "linux", "bash",
    "git", "llm", "openai", "langchain", "rag", "prompt engineering",
    "machine learning", "deep learning", "pandas", "numpy", "pytorch",
    "tensorflow", "microservices", "rest api", "graphql", "rabbitmq",
    "kafka", "elasticsearch", "nginx", "scikit-learn", "transformers",
]


def extract_text(path: Path) -> str:
    """Return plain text from a PDF or .txt file."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n\n".join(pages).strip()
        if not text:
            raise ValueError(
                f"No extractable text found in {path.name} "
                "(may be a scanned/image-only PDF)."
            )
        return text
    if suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace").strip()
    raise ValueError("Unsupported file type. Provide a .pdf, .txt, or .md file.")


def extract_skills(raw_text: str) -> list[str]:
    """Lightweight skill detection: case-insensitive keyword scan."""
    lowered = raw_text.lower()
    found = []
    for skill in COMMON_SKILLS:
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(skill)}(?![a-z0-9])")
        if pattern.search(lowered):
            found.append(skill)
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest a resume into Supabase.")
    parser.add_argument("resume_path", type=Path, help="Path to a .pdf, .txt, or .md file")
    args = parser.parse_args()

    if not args.resume_path.exists():
        sys.exit(f"Error: file not found: {args.resume_path}")

    print(f"Extracting text from {args.resume_path.name}...")
    raw_text = extract_text(args.resume_path)
    skills = extract_skills(raw_text)
    print(f"Extracted {len(raw_text)} characters, detected {len(skills)} skills.")

    client = get_client()
    row = (
        client.table("resume")
        .insert({
            "raw_text": raw_text,
            "parsed_skills": json.dumps(skills),
        })
        .execute()
    )
    inserted = row.data[0]
    print(f"OK - stored resume row id={inserted['id']} "
          f"(uploaded_at={inserted['uploaded_at']})")
    if skills:
        print(f"Detected skills: {', '.join(skills)}")


if __name__ == "__main__":
    main()
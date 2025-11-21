import os
import re
import json
from urllib.parse import urlparse, parse_qs
from typing import List, Dict, Any, Optional

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from xml.etree import ElementTree as ET

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {"message": "YouTube Clip Suggester API running"}


@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}


@app.get("/test")
def test_database():
    """Basic health + environment check (DB optional for this app)."""
    response = {
        "backend": "✅ Running",
        "database": "ℹ️ Not used",
        "database_url": "❌ Not Set",
        "database_name": "❌ Not Set",
        "connection_status": "N/A",
        "collections": [],
    }
    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
    return response


# ---------- Helpers ----------

def extract_video_id(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        if parsed.netloc.endswith("youtube.com"):
            if parsed.path == "/watch":
                return parse_qs(parsed.query).get("v", [None])[0]
            # shorts
            if parsed.path.startswith("/shorts/"):
                return parsed.path.split("/")[-1]
        if parsed.netloc == "youtu.be":
            return parsed.path.strip("/")
    except Exception:
        return None
    return None


def fetch_transcript_xml(video_id: str, lang: str = "en") -> Optional[str]:
    # Try exact lang, then autosubs
    endpoints = [
        f"https://www.youtube.com/api/timedtext?v={video_id}&lang={lang}",
        f"https://www.youtube.com/api/timedtext?v={video_id}&lang={lang}&fmt=vtt",
        f"https://www.youtube.com/api/timedtext?v={video_id}&lang=en",
        f"https://www.youtube.com/api/timedtext?v={video_id}&lang=en&fmt=vtt",
    ]
    for url in endpoints:
        r = requests.get(url, timeout=10)
        if r.status_code == 200 and r.text.strip():
            return r.text
    return None


def parse_timedtext(xml_text: str) -> List[Dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
        segments: List[Dict[str, Any]] = []
        # Elements are <text start=".." dur="..">text</text>
        for node in root.iter("text"):
            start = float(node.attrib.get("start", "0"))
            dur = float(node.attrib.get("dur", "0"))
            # Unescape HTML entities and replace newlines
            text = (node.text or "").replace("\n", " ")
            segments.append({"start": start, "dur": dur, "end": start + dur, "text": text})
        return segments
    except Exception:
        return []


def suggest_clips_from_segments(segments: List[Dict[str, Any]], min_len: float = 20.0, max_len: float = 60.0, top_k: int = 3) -> List[Dict[str, Any]]:
    if not segments:
        return []

    # Simple heuristic: build windows around pause boundaries and punctuation
    keywords = [
        "secret", "tips", "hack", "mistake", "story", "crazy", "insane", "unexpected",
        "how to", "why", "what", "this is", "you need", "stop", "start", "learn",
        "viral", "trick", "strategy", "trend", "money", "growth", "win", "best",
    ]

    candidates: List[Dict[str, Any]] = []
    n = len(segments)
    i = 0
    while i < n:
        # start a window
        j = i
        window_text = []
        start_t = segments[i]["start"]
        while j < n and segments[j]["end"] - start_t < max_len:
            window_text.append(segments[j]["text"])
            # Prefer to break on punctuation and if we've exceeded min_len
            if segments[j]["end"] - start_t >= min_len:
                combined = " ".join(window_text)
                if re.search(r"[.!?]", segments[j]["text"]) or (j + 1 < n and segments[j + 1]["start"] - segments[j]["end"] > 0.6):
                    # Score by keyword hits and brevity
                    kw_score = sum(2 for k in keywords if k in combined.lower())
                    length_penalty = abs((segments[j]["end"] - start_t) - ((min_len + max_len) / 2)) / 10.0
                    score = kw_score - length_penalty
                    candidates.append({
                        "start": round(start_t, 2),
                        "end": round(segments[j]["end"], 2),
                        "duration": round(segments[j]["end"] - start_t, 2),
                        "text": combined.strip(),
                        "score": score,
                    })
                    break
            j += 1
        i = max(i + 1, j)  # move forward

    # Deduplicate overlapping by greedy selection
    candidates.sort(key=lambda x: x["score"], reverse=True)
    selected: List[Dict[str, Any]] = []
    used = []
    for c in candidates:
        if len(selected) >= top_k:
            break
        overlap = False
        for u in used:
            if not (c["end"] <= u["start"] or c["start"] >= u["end"]):
                overlap = True
                break
        if not overlap:
            selected.append(c)
            used.append({"start": c["start"], "end": c["end"]})

    return selected


# ---------- API Endpoints ----------

@app.get("/scrape_links")
def scrape_links(url: str = Query(..., description="YouTube channel, playlist, or page URL")):
    try:
        r = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (compatible; ClipSuggester/1.0)"
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {e}")

    if r.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Non-200 status: {r.status_code}")

    html = r.text
    # Find video IDs in HTML
    ids = set(re.findall(r"watch\\?v=([A-Za-z0-9_-]{11})", html))
    short_ids = set(re.findall(r"/shorts/([A-Za-z0-9_-]{11})", html))
    all_ids = list(ids.union(short_ids))

    links = [f"https://www.youtube.com/watch?v={vid}" for vid in all_ids]
    return {"count": len(links), "links": links[:50]}  # limit to 50


@app.get("/transcript")
def transcript(video_id: Optional[str] = None, url: Optional[str] = None, lang: str = "en"):
    if not video_id and not url:
        raise HTTPException(status_code=400, detail="Provide video_id or url")
    if url and not video_id:
        video_id = extract_video_id(url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Unable to extract video id")

    xml_text = fetch_transcript_xml(video_id, lang)
    if not xml_text:
        return JSONResponse({"video_id": video_id, "segments": [], "available": False})

    segments = parse_timedtext(xml_text)
    return {"video_id": video_id, "segments": segments, "available": True}


@app.get("/suggest_clips")
def suggest_clips(video_id: Optional[str] = None, url: Optional[str] = None, lang: str = "en", top_k: int = 3):
    if url and not video_id:
        video_id = extract_video_id(url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Provide video_id or url")

    xml_text = fetch_transcript_xml(video_id, lang)
    if not xml_text:
        return JSONResponse({"video_id": video_id, "clips": [], "available": False})

    segments = parse_timedtext(xml_text)
    clips = suggest_clips_from_segments(segments, top_k=top_k)

    # Gather lines within each clip window for subtitles
    results = []
    for c in clips:
        lines = [s for s in segments if s["start"] >= c["start"] and s["end"] <= c["end"]]
        results.append({
            "start": c["start"],
            "end": c["end"],
            "duration": c["duration"],
            "text": c["text"],
            "lines": lines,
        })

    return {"video_id": video_id, "clips": results, "available": True}


@app.get("/oembed")
def oembed_proxy(url: str):
    # Simple metadata proxy (title/thumbnail) using YouTube's oEmbed
    oembed_url = f"https://www.youtube.com/oembed?url={requests.utils.quote(url)}&format=json"
    r = requests.get(oembed_url, timeout=10)
    if r.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch metadata")
    return JSONResponse(content=r.json())


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

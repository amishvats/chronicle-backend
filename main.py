from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import create_engine, text
import json
from dotenv import load_dotenv
import os
from openai import OpenAI
from datetime import date, timedelta


load_dotenv()
app = FastAPI()
client = OpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1"
)
engine = create_engine("postgresql://postgres:chronicle123@localhost:5432/chronicle")

PLACE_TO_REGION = {
    "surat": "Western India",
    "panipat": "North India",
    "kannauj": "North India",
    "agra": "North India",
    "pune": "Western India"
}

class PlaythroughRequest(BaseModel):
    place: str
    year: int
    role: str

@app.post("/playthroughs")
def create_playthrough(data: PlaythroughRequest):
    response = client.chat.completions.create(
        model="anthropic/claude-sonnet-4.5",
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": f"Create a fictional character living in {data.place} in the year {data.year}, working as a {data.role}. Respond with ONLY a JSON object with these exact fields: name, age, occupation, family (list), location, wealth_level, health, personality_traits (list), relationships (list of objects with name/role/sentiment), life_flags (object with married/has_children/displaced booleans), recent_events_seen (empty list). No extra text, just the JSON."
            }
        ]
    )
    raw_content = response.choices[0].message.content
    raw_content = raw_content.strip()
    if raw_content.startswith("```"):
        raw_content = raw_content.split("```")[1]
        if raw_content.startswith("json"):
            raw_content = raw_content[4:]
    character_state = json.loads(raw_content.strip())

    region = PLACE_TO_REGION.get(data.place.lower())
    if region is None:
        region_response = client.chat.completions.create(
            model="anthropic/claude-sonnet-4.5",
            max_tokens=20,
            messages=[
                {
                    "role": "user",
                    "content": f"What broad Indian historical region does the city '{data.place}' belong to? Answer with ONLY one of these exact words: Western India, North India, Eastern India, South India, Central India. No other text."
                }
            ]
        )
        region = region_response.choices[0].message.content.strip()

    with engine.connect() as connection:
        result = connection.execute(
            text("""
                INSERT INTO playthroughs (place, region, start_year, role, character_state, current_game_date, seed)
                VALUES (:place, :region, :start_year, :role, :character_state, :current_game_date, :seed)
                RETURNING id
            """),
            {
                "place": data.place,
                "region": region,
                "start_year": data.year,
                "role": data.role,
                "character_state": json.dumps(character_state),
                "current_game_date": f"{data.year}-01-01",
                "seed": 12345
            }
        )
        connection.commit()
        new_id = result.fetchone()[0]
        return {"playthrough_id": new_id, "character_state": character_state}

@app.get("/test-db")
def test_db():
    connection = engine.connect()
    connection.close()
    return {"status": "connected"}

@app.get("/playthroughs/{playthrough_id}")
def get_playthrough(playthrough_id: str):
    with engine.connect() as connection:
        result = connection.execute(
            text("SELECT * FROM playthroughs WHERE id = :playthrough_id"),
            {"playthrough_id": playthrough_id}
        )
        row = result.fetchone()
        return {
            "id": row.id,
            "place": row.place,
            "role": row.role,
            "character_state": row.character_state
        }

class AdvanceRequest(BaseModel):
    skip: str

@app.get("/events")
def get_events():
    with engine.connect() as connection:
        result = connection.execute(text("SELECT * FROM historical_events"))
        rows = result.fetchall()
        events = []
        for row in rows:
            events.append({
                "id": row.id,
                "title": row.title,
                "description": row.description,
                "region": row.region,
                "start_date": str(row.start_date),
                "end_date": str(row.end_date),
                "severity": row.severity,
                "category": row.category
            })
        return events

@app.post("/playthroughs/{playthrough_id}/advance")
def advance_playthrough(playthrough_id: str, data: AdvanceRequest):
    with engine.connect() as connection:
        result = connection.execute(
            text("SELECT * FROM playthroughs WHERE id = :playthrough_id"),
            {"playthrough_id": playthrough_id}
        )
        row = result.fetchone()

        current_date = row.current_game_date

        if data.skip == "1d":
            new_date = current_date + timedelta(days=1)
        elif data.skip == "1w":
            new_date = current_date + timedelta(weeks=1)
        elif data.skip == "1m":
            new_date = current_date + timedelta(days=30)
        else:
            new_date = current_date

        events_result = connection.execute(
            text("""
                SELECT * FROM historical_events 
                WHERE region = :region 
                AND start_date BETWEEN :start AND :end
            """),
            {
                "region": row.region,
                "start": current_date,
                "end": new_date
            }
        )
        matching_events = events_result.fetchall()

        if len(matching_events) > 0:
            event_details = [f"{e.title}: {e.description}" for e in matching_events]
            events_text = " ".join(event_details)

            character_name = json.loads(row.character_state)["name"] if isinstance(row.character_state, str) else row.character_state["name"]

            narrative_response = client.chat.completions.create(
                model="anthropic/claude-sonnet-4.5",
                max_tokens=300,
                messages=[
                    {
                        "role": "user",
                        "content": f"Write a short first-person diary entry (2-4 sentences) for a character named {character_name}, a {row.role} living in {row.place}. The following historical events happened during this period: {events_text}. Write it as if the character is personally experiencing/hearing about these events. Only dramatize what's given, don't invent new facts."
                    }
                ]
            )
            narrative = narrative_response.choices[0].message.content.strip()
        else:
            narrative = "A quiet period passed, with life continuing steadily."

        event_ids = [e.id for e in matching_events]

        connection.execute(
            text("""
                INSERT INTO entries (playthrough_id, date_range_start, date_range_end, narrative, events_referenced)
                VALUES (:playthrough_id, :start, :end, :narrative, :event_ids)
            """),
            {
                "playthrough_id": playthrough_id,
                "start": current_date,
                "end": new_date,
                "narrative": narrative,
                "event_ids": event_ids
            }
        )

        connection.execute(
            text("UPDATE playthroughs SET current_game_date = :new_date WHERE id = :playthrough_id"),
            {"new_date": new_date, "playthrough_id": playthrough_id}
        )

        connection.commit()

        return {
            "narrative": narrative,
            "new_date": str(new_date),
            "events": event_ids
        }
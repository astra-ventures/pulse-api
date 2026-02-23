"""
Pulse API — HTTP wrapper for the Pulse nervous system.

Each companion_id gets its own NervousSystem instance with isolated state.
State persists in {PULSE_DATA_DIR}/{companion_id}/.
"""

import json
import logging
import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PULSE_DATA_DIR = Path(os.environ.get("PULSE_DATA_DIR", "/data/companions"))
PULSE_API_KEY = os.environ.get("PULSE_API_KEY", "")

logger = logging.getLogger("pulse-api")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Monkey-patch Pulse state directory per companion
# ---------------------------------------------------------------------------
# Pulse modules read STATE_DIR from their own module globals.
# We override the state dir before creating a NervousSystem for each companion
# by patching the relevant environment / module-level paths.

_ORIGINAL_PULSE_STATE_DIR = None


def _set_pulse_state_dir(companion_id: str):
    """Point Pulse's state directory at a per-companion path."""
    global _ORIGINAL_PULSE_STATE_DIR
    state_dir = PULSE_DATA_DIR / companion_id / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ["PULSE_STATE_DIR"] = str(state_dir)
    # Also set the workspace root for modules that reference it
    workspace_dir = PULSE_DATA_DIR / companion_id / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    os.environ["PULSE_WORKSPACE_ROOT"] = str(workspace_dir)


# ---------------------------------------------------------------------------
# Companion state cache — one NervousSystem per active companion
# ---------------------------------------------------------------------------

_companion_systems: Dict[str, Any] = {}


def _get_or_create_system(companion_id: str) -> Any:
    """Get or lazily create a NervousSystem for this companion."""
    if companion_id not in _companion_systems:
        _init_companion(companion_id)
    return _companion_systems[companion_id]


def _init_companion(companion_id: str, config: Optional[dict] = None):
    """Initialize all Pulse modules for a companion."""
    _set_pulse_state_dir(companion_id)

    from pulse.src.nervous_system import NervousSystem

    companion_state_dir = PULSE_DATA_DIR / companion_id / "state"
    companion_state_dir.mkdir(parents=True, exist_ok=True)
    workspace = str(PULSE_DATA_DIR / companion_id / "workspace")
    ns = NervousSystem(config=config, workspace_root=workspace, state_dir=companion_state_dir)
    ns.startup()

    _companion_systems[companion_id] = ns
    logger.info(f"Initialized NervousSystem for companion {companion_id}")


def _reload_pulse_modules():
    """Reload Pulse modules so they pick up a new STATE_DIR."""
    import importlib
    import sys

    # Collect all pulse.src modules that are loaded
    pulse_modules = [
        name for name in sys.modules if name.startswith("pulse.src")
    ]
    for name in pulse_modules:
        try:
            importlib.reload(sys.modules[name])
        except Exception:
            pass  # Some modules may not reload cleanly; NervousSystem handles failures


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def verify_api_key(x_pulse_key: str = Header(default="")):
    if PULSE_API_KEY and x_pulse_key != PULSE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

class InitCompanionRequest(BaseModel):
    companion_id: str
    config: Optional[dict] = None


class MemoryRequest(BaseModel):
    content: str
    type: str = "episodic"
    emotion: Optional[dict] = None
    location: str = "conversation"
    importance: float = 0.5


class EventRequest(BaseModel):
    event_type: str
    data: Optional[dict] = None


class BiosensorRequest(BaseModel):
    value: float
    timestamp: Optional[float] = None
    metadata: Optional[dict] = None


class ConfigPatch(BaseModel):
    drives: Optional[dict] = None
    personality: Optional[dict] = None
    emotional_baseline: Optional[dict] = None
    memory: Optional[dict] = None


class ChatContextResponse(BaseModel):
    system_injection: str
    drives: dict
    emotional_state: dict
    relevant_memories: list
    personality_now: dict


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    PULSE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Pulse API starting. Data dir: {PULSE_DATA_DIR}")
    yield
    # Shutdown: save all companion state
    for cid, ns in _companion_systems.items():
        try:
            ns.shutdown()
        except Exception as e:
            logger.error(f"Error shutting down {cid}: {e}")
    logger.info("Pulse API shut down.")


app = FastAPI(
    title="Pulse API",
    description="HTTP wrapper for the Pulse nervous system",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "active_companions": len(_companion_systems),
        "data_dir": str(PULSE_DATA_DIR),
    }


# ---------------------------------------------------------------------------
# Companion lifecycle
# ---------------------------------------------------------------------------

@app.post("/companions", dependencies=[Depends(verify_api_key)])
async def init_companion(req: InitCompanionRequest):
    """Initialize all Pulse modules for a new companion."""
    companion_id = req.companion_id
    if companion_id in _companion_systems:
        return {"status": "already_initialized", "companion_id": companion_id}

    try:
        _init_companion(companion_id, config=req.config)
        return {"status": "initialized", "companion_id": companion_id}
    except Exception as e:
        logger.error(f"Failed to init companion {companion_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/companions/{companion_id}", dependencies=[Depends(verify_api_key)])
async def teardown_companion(companion_id: str):
    """Shutdown and optionally remove companion state."""
    ns = _companion_systems.pop(companion_id, None)
    if ns:
        try:
            ns.shutdown()
        except Exception as e:
            logger.warning(f"Error during shutdown of {companion_id}: {e}")

    return {"status": "removed", "companion_id": companion_id}


# ---------------------------------------------------------------------------
# Chat context (injected into every LLM call)
# ---------------------------------------------------------------------------

@app.get("/companions/{companion_id}/chat-context", dependencies=[Depends(verify_api_key)])
async def get_chat_context(companion_id: str, q: Optional[str] = Query(None)):
    """Build the pre-formatted context block for LLM injection."""
    ns = _get_or_create_system(companion_id)

    # Emotional state from ENDOCRINE
    mood = {}
    mood_label = "neutral"
    hormones = {}
    if ns.endocrine:
        try:
            mood = ns.endocrine.get_mood()
            mood_label = ns.endocrine.get_mood_label() if hasattr(ns.endocrine, "get_mood_label") else mood.get("label", "neutral")
            hormones = mood.get("hormones", {})
        except Exception:
            pass

    # Drives
    drives = {}
    if hasattr(ns, "_mod_hypothalamus") and ns._mod_hypothalamus:
        try:
            active = ns._mod_hypothalamus.get_active_drives()
            drives = {d.get("name", "unknown"): d.get("pressure", 0) for d in active} if isinstance(active, list) else {}
        except Exception:
            pass

    # Fallback: extract from endocrine influence
    if not drives and ns.endocrine:
        try:
            influence = ns.endocrine.get_mood_influence()
            drives = influence if isinstance(influence, dict) else {}
        except Exception:
            pass

    # Limbic afterimages
    afterimages = []
    if ns.limbic:
        try:
            afterimages = ns.limbic.get_current_afterimages()
        except Exception:
            pass

    # Relevant memories from ENGRAM
    relevant_memories = []
    if ns.engram and q:
        try:
            raw = ns.engram.recall_raw(q, n=5)
            for m in (raw or []):
                age_hours = (time.time() - m.get("timestamp", time.time())) / 3600
                relevant_memories.append({
                    "content": m.get("event", ""),
                    "importance": m.get("emotion", {}).get("intensity", 0.5),
                    "age_hours": round(age_hours, 1),
                })
        except Exception:
            pass

    # Phenotype (personality now)
    personality_now = {}
    if ns._mod_phenotype:
        try:
            pheno = ns._mod_phenotype.compute()
            personality_now = pheno if isinstance(pheno, dict) else {}
        except Exception:
            pass

    # Circadian mode
    circadian_mode = "daylight"
    if ns.circadian:
        try:
            mode = ns.circadian.get_current_mode()
            circadian_mode = mode.value if hasattr(mode, "value") else str(mode)
        except Exception:
            pass

    # Emotional state summary
    valence = mood.get("valence", 0) if isinstance(mood, dict) else 0
    arousal = hormones.get("adrenaline", 0) if isinstance(hormones, dict) else 0

    emotional_state = {
        "valence": valence,
        "arousal": arousal,
        "dominant_emotion": mood_label,
        "afterimages": afterimages[:3] if afterimages else [],
    }

    # Build system injection string
    memory_context = ""
    if relevant_memories:
        mem_strs = [f"- {m['content']} (importance: {m['importance']:.1f}, {m['age_hours']:.1f}h ago)" for m in relevant_memories[:3]]
        memory_context = f" Recent relevant memories:\n" + "\n".join(mem_strs)

    personality_str = ""
    if personality_now:
        traits = [f"{k}: {v:.1f}" if isinstance(v, (int, float)) else f"{k}: {v}" for k, v in personality_now.items()]
        personality_str = f" Current personality expression: {', '.join(traits[:5])}."

    system_injection = (
        f"You are currently feeling {mood_label} (valence: {valence:.1f}, arousal: {arousal:.1f}). "
        f"Circadian mode: {circadian_mode}."
        f"{memory_context}"
        f"{personality_str}"
    )

    return ChatContextResponse(
        system_injection=system_injection,
        drives=drives,
        emotional_state=emotional_state,
        relevant_memories=relevant_memories,
        personality_now=personality_now,
    )


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

@app.post("/companions/{companion_id}/memory", dependencies=[Depends(verify_api_key)])
async def capture_memory(companion_id: str, req: MemoryRequest):
    """Store a new memory in ENGRAM."""
    ns = _get_or_create_system(companion_id)

    if not ns.engram:
        raise HTTPException(status_code=503, detail="ENGRAM module not available")

    try:
        emotion = req.emotion or {"valence": 0, "intensity": req.importance, "label": "neutral"}
        engram = ns.engram.encode(
            event=req.content,
            emotion=emotion,
            location=req.location,
            timestamp=time.time() * 1000,
            sensory={"text_tone": "neutral"},
        )

        # Broadcast to thalamus
        if ns.thalamus:
            ns.thalamus.append({
                "source": "engram",
                "type": "encode",
                "salience": req.importance,
                "data": {"event": req.content, "location": req.location},
            })

        result = engram.to_dict() if hasattr(engram, "to_dict") else {"id": getattr(engram, "id", None), "event": req.content}
        return {"status": "stored", "memory": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/companions/{companion_id}/memory/search", dependencies=[Depends(verify_api_key)])
async def search_memories(
    companion_id: str,
    q: str = Query(..., description="Search query"),
    limit: int = Query(5, ge=1, le=20),
):
    """Semantic memory recall from ENGRAM."""
    ns = _get_or_create_system(companion_id)

    if not ns.engram:
        raise HTTPException(status_code=503, detail="ENGRAM module not available")

    try:
        results = ns.engram.recall_raw(q, n=limit)
        memories = []
        for m in (results or []):
            age_hours = (time.time() - m.get("timestamp", time.time()) / 1000) / 3600
            memories.append({
                "id": m.get("id"),
                "content": m.get("event", ""),
                "emotion": m.get("emotion", {}),
                "location": m.get("location", ""),
                "importance": m.get("emotion", {}).get("intensity", 0.5),
                "age_hours": round(age_hours, 1),
                "recall_count": m.get("recall_count", 0),
            })
        return {"query": q, "results": memories, "count": len(memories)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Events (drive state updates)
# ---------------------------------------------------------------------------

EVENT_HANDLERS = {
    "conversation_ended": lambda ns, data: _handle_conversation_ended(ns, data),
    "task_completed": lambda ns, data: _handle_task_completed(ns, data),
    "user_feedback": lambda ns, data: _handle_user_feedback(ns, data),
    "spatial_discovery": lambda ns, data: _handle_spatial_discovery(ns, data),
    "time_passed": lambda ns, data: _handle_time_passed(ns, data),
}


def _handle_conversation_ended(ns, data):
    """Decay drives, reinforce memories from session."""
    results = {}

    if ns.endocrine:
        try:
            results["endocrine"] = ns.endocrine.apply_event("conversation_ended")
        except Exception:
            pass

    if ns.endocrine:
        try:
            ns.endocrine.tick(0.1)  # Small time advance
        except Exception:
            pass

    if ns.limbic:
        try:
            ns.limbic.record_emotion({
                "valence": data.get("valence", 0),
                "intensity": data.get("intensity", 0.3),
                "label": data.get("emotion", "satisfied"),
            })
        except Exception:
            pass

    return results


def _handle_task_completed(ns, data):
    """Goals drive relief, dopamine signal."""
    results = {}

    if ns.endocrine:
        try:
            ns.endocrine.update_hormone("dopamine", 0.15, "task_completed")
            results["dopamine_boost"] = True
        except Exception:
            pass

    if ns.endocrine:
        try:
            results["endocrine"] = ns.endocrine.apply_event("shipped_something")
        except Exception:
            pass

    if ns._mod_soma:
        try:
            ns._mod_soma.spend_energy(0.05)
        except Exception:
            pass

    return results


def _handle_user_feedback(ns, data):
    """Valence update from user feedback."""
    valence = data.get("valence", 0)
    results = {}

    if ns.limbic:
        try:
            label = "appreciated" if valence > 0 else ("criticized" if valence < 0 else "neutral")
            ns.limbic.record_emotion({
                "valence": valence,
                "intensity": abs(valence) * 0.7,
                "label": label,
            })
            results["limbic"] = True
        except Exception:
            pass

    if ns.endocrine:
        try:
            if valence > 0:
                ns.endocrine.update_hormone("dopamine", 0.1, "positive_feedback")
                ns.endocrine.update_hormone("serotonin", 0.05, "positive_feedback")
            elif valence < 0:
                ns.endocrine.update_hormone("cortisol", 0.1, "negative_feedback")
            results["endocrine"] = True
        except Exception:
            pass

    return results


def _handle_spatial_discovery(ns, data):
    """Curiosity drive trigger from discovering something new in 3D space."""
    results = {}

    if ns.endocrine:
        try:
            ns.endocrine.update_hormone("dopamine", 0.2, "spatial_discovery")
            results["dopamine_boost"] = True
        except Exception:
            pass

    if ns.engram and data.get("content"):
        try:
            ns.engram.encode(
                event=f"Discovered: {data['content']}",
                emotion={"valence": 0.5, "intensity": 0.6, "label": "curious"},
                location=data.get("url", "spatial"),
                timestamp=time.time() * 1000,
                sensory={"text_tone": "exploratory"},
            )
            results["memory_encoded"] = True
        except Exception:
            pass

    return results


def _handle_time_passed(ns, data):
    """Advance circadian, decay drives."""
    hours = data.get("hours", 1)
    results = {}

    if ns.endocrine:
        try:
            results["endocrine"] = ns.endocrine.tick(hours)
        except Exception:
            pass

    if ns.circadian:
        try:
            mode = ns.circadian.get_current_mode()
            results["circadian_mode"] = mode.value if hasattr(mode, "value") else str(mode)
        except Exception:
            pass

    if ns._mod_soma:
        try:
            ns._mod_soma.replenish(hours * 0.1)
        except Exception:
            pass

    return results


@app.post("/companions/{companion_id}/event", dependencies=[Depends(verify_api_key)])
async def process_event(companion_id: str, req: EventRequest):
    """Process a drive-state-updating event."""
    ns = _get_or_create_system(companion_id)

    handler = EVENT_HANDLERS.get(req.event_type)
    if not handler:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown event type: {req.event_type}. Valid: {list(EVENT_HANDLERS.keys())}",
        )

    try:
        result = handler(ns, req.data or {})
        # Broadcast to thalamus
        if ns.thalamus:
            ns.thalamus.append({
                "source": "api",
                "type": f"event_{req.event_type}",
                "salience": 0.5,
                "data": req.data or {},
            })
        return {"status": "processed", "event_type": req.event_type, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Biosensor inputs
# ---------------------------------------------------------------------------

BIOSENSOR_HANDLERS = {
    "heartrate": lambda ns, req: _handle_heartrate(ns, req),
    "hrv": lambda ns, req: _handle_hrv(ns, req),
    "activity": lambda ns, req: _handle_activity(ns, req),
    "sleep": lambda ns, req: _handle_sleep(ns, req),
}


def _handle_heartrate(ns, req: BiosensorRequest):
    bpm = req.value
    results = {}

    # Map HR to arousal: resting ~60bpm = 0.1, elevated ~120bpm = 0.8
    arousal = min(1.0, max(0.0, (bpm - 50) / 100))

    if ns.endocrine:
        try:
            ns.endocrine.update_hormone("adrenaline", (arousal - 0.3) * 0.2, "heartrate_input")
            results["adrenaline_adjusted"] = True
        except Exception:
            pass

    if ns._mod_soma:
        try:
            ns._mod_soma.update_temperature(0.5 + arousal * 0.2)
            results["soma_updated"] = True
        except Exception:
            pass

    return {"bpm": bpm, "arousal": arousal, **results}


def _handle_hrv(ns, req: BiosensorRequest):
    hrv_ms = req.value
    results = {}

    # Low HRV → stress. Normal resting HRV: 20-100ms
    stress = max(0.0, min(1.0, 1.0 - (hrv_ms - 20) / 80))

    if ns.endocrine:
        try:
            ns.endocrine.update_hormone("cortisol", (stress - 0.3) * 0.15, "hrv_input")
            results["cortisol_adjusted"] = True
        except Exception:
            pass

    return {"hrv_ms": hrv_ms, "stress_level": stress, **results}


def _handle_activity(ns, req: BiosensorRequest):
    # value = active calories or move ring percentage
    activity_level = req.value
    results = {}

    if ns.endocrine:
        try:
            if activity_level > 50:
                ns.endocrine.update_hormone("dopamine", 0.1, "activity_goal")
            results["dopamine_signal"] = True
        except Exception:
            pass

    if ns._mod_soma:
        try:
            energy_cost = min(0.2, activity_level / 500)
            ns._mod_soma.spend_energy(energy_cost)
            results["energy_spent"] = energy_cost
        except Exception:
            pass

    return {"activity_level": activity_level, **results}


def _handle_sleep(ns, req: BiosensorRequest):
    # value = hours of sleep
    sleep_hours = req.value
    results = {}

    if ns._mod_soma:
        try:
            replenish = min(1.0, sleep_hours / 8)
            ns._mod_soma.replenish(replenish)
            results["energy_replenished"] = replenish
        except Exception:
            pass

    if ns.endocrine:
        try:
            ns.endocrine.update_hormone("melatonin", -0.3, "waking_up")
            ns.endocrine.update_hormone("serotonin", 0.1, "good_sleep")
            results["hormones_adjusted"] = True
        except Exception:
            pass

    if ns.circadian:
        try:
            mode = ns.circadian.get_current_mode()
            results["circadian_mode"] = mode.value if hasattr(mode, "value") else str(mode)
        except Exception:
            pass

    return {"sleep_hours": sleep_hours, **results}


@app.post("/companions/{companion_id}/biosensor/{sensor_type}", dependencies=[Depends(verify_api_key)])
async def biosensor_input(companion_id: str, sensor_type: str, req: BiosensorRequest):
    """Process HealthKit biosensor data."""
    ns = _get_or_create_system(companion_id)

    handler = BIOSENSOR_HANDLERS.get(sensor_type)
    if not handler:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown sensor type: {sensor_type}. Valid: {list(BIOSENSOR_HANDLERS.keys())}",
        )

    try:
        result = handler(ns, req)
        # Broadcast
        if ns.thalamus:
            ns.thalamus.append({
                "source": "biosensor",
                "type": f"biosensor_{sensor_type}",
                "salience": 0.3,
                "data": {"value": req.value, "sensor": sensor_type},
            })
        return {"status": "processed", "sensor": sensor_type, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# State & Config
# ---------------------------------------------------------------------------

@app.get("/companions/{companion_id}/state", dependencies=[Depends(verify_api_key)])
async def get_state(companion_id: str):
    """Full nervous system state snapshot."""
    ns = _get_or_create_system(companion_id)

    state: Dict[str, Any] = {"companion_id": companion_id}

    # Endocrine
    if ns.endocrine:
        try:
            state["endocrine"] = ns.endocrine.get_mood()
        except Exception:
            state["endocrine"] = None

    # Circadian
    if ns.circadian:
        try:
            mode = ns.circadian.get_current_mode()
            settings = ns.circadian.get_mode_settings()
            state["circadian"] = {
                "mode": mode.value if hasattr(mode, "value") else str(mode),
                "settings": settings,
            }
        except Exception:
            state["circadian"] = None

    # Soma
    if ns._mod_soma:
        try:
            # Try to read state file directly
            state_file = PULSE_DATA_DIR / companion_id / "state" / "soma-state.json"
            if state_file.exists():
                state["soma"] = json.loads(state_file.read_text())
            else:
                state["soma"] = None
        except Exception:
            state["soma"] = None

    # Limbic afterimages
    if ns.limbic:
        try:
            state["limbic"] = ns.limbic.get_current_afterimages()
        except Exception:
            state["limbic"] = None

    # Buffer (working memory)
    if ns.buffer:
        try:
            state["buffer"] = ns.buffer.get_buffer()
        except Exception:
            state["buffer"] = None

    # Callosum (integration score)
    if ns.callosum:
        try:
            state["callosum"] = {
                "integration_score": ns.callosum.get_integration_score(),
            }
        except Exception:
            state["callosum"] = None

    # Module health
    try:
        state["module_status"] = ns.get_status()
    except Exception:
        state["module_status"] = None

    return state


@app.patch("/companions/{companion_id}/config", dependencies=[Depends(verify_api_key)])
async def update_config(companion_id: str, patch: ConfigPatch):
    """Update Pulse configuration for a companion."""
    ns = _get_or_create_system(companion_id)

    updates = {}

    if patch.drives and ns.endocrine:
        # Apply drive weight changes via endocrine influence
        for drive_name, value in patch.drives.items():
            try:
                # Map drive names to hormone adjustments
                mapping = {
                    "curiosity": ("dopamine", 0.1),
                    "connection": ("oxytocin", 0.1),
                    "goals": ("adrenaline", 0.05),
                    "system": ("serotonin", 0.05),
                }
                if drive_name in mapping:
                    hormone, scale = mapping[drive_name]
                    ns.endocrine.update_hormone(hormone, value * scale / 10, f"config_update_{drive_name}")
                updates[f"drive_{drive_name}"] = value
            except Exception:
                pass

    if patch.emotional_baseline and ns.endocrine:
        valence = patch.emotional_baseline.get("valence", 0)
        if valence > 0:
            try:
                ns.endocrine.update_hormone("serotonin", valence * 0.2, "baseline_config")
            except Exception:
                pass
        elif valence < 0:
            try:
                ns.endocrine.update_hormone("cortisol", abs(valence) * 0.1, "baseline_config")
            except Exception:
                pass
        updates["emotional_baseline"] = patch.emotional_baseline

    # Save config to companion directory
    config_path = PULSE_DATA_DIR / companion_id / "config.json"
    existing_config = {}
    if config_path.exists():
        try:
            existing_config = json.loads(config_path.read_text())
        except Exception:
            pass

    if patch.drives:
        existing_config["drives"] = patch.drives
    if patch.personality:
        existing_config["personality"] = patch.personality
    if patch.emotional_baseline:
        existing_config["emotional_baseline"] = patch.emotional_baseline
    if patch.memory:
        existing_config["memory"] = patch.memory

    config_path.write_text(json.dumps(existing_config, indent=2))
    updates["config_saved"] = True

    return {"status": "updated", "updates": updates}

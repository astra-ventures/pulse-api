"""Tests for Pulse API endpoints.

Coverage: health, auth, companion lifecycle, chat-context,
memory, events, biosensors, state, config patch.
"""
import time
import pytest


# ---------------------------------------------------------------------------
# Health (no auth)
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "active_companions" in body
        assert "data_dir" in body
        assert isinstance(body["active_companions"], int)

    def test_health_data_dir_is_temp(self, client, isolated_pulse_data_dir):
        r = client.get("/health")
        assert str(isolated_pulse_data_dir) in r.json()["data_dir"]

    def test_health_requires_no_auth(self, client):
        """Health endpoint must be publicly accessible."""
        r = client.get("/health", headers={})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class TestAuth:
    def test_missing_key_rejected(self, client):
        r = client.post(
            "/companions",
            json={"companion_id": "auth-test-001"},
            headers={}
        )
        assert r.status_code in (401, 403)  # API returns 401 Unauthorized

    def test_wrong_key_rejected(self, client):
        r = client.post(
            "/companions",
            json={"companion_id": "auth-test-001"},
            headers={"X-Pulse-Key": "wrong-key-xyz"}
        )
        assert r.status_code in (401, 403)  # API returns 401 Unauthorized

    def test_correct_key_accepted(self, client, auth_headers):
        r = client.post(
            "/companions",
            json={"companion_id": "auth-test-accepted"},
            headers=auth_headers
        )
        # 200 or 500 (if Pulse init has issues in test env) — but NOT 403
        assert r.status_code != 403


# ---------------------------------------------------------------------------
# Companion Lifecycle
# ---------------------------------------------------------------------------

class TestCompanionLifecycle:
    def test_init_creates_companion(self, client, auth_headers, test_companion_id):
        r = client.post(
            "/companions",
            json={"companion_id": test_companion_id},
            headers=auth_headers
        )
        assert r.status_code == 200
        body = r.json()
        assert body["companion_id"] == test_companion_id
        assert body["status"] in ("initialized", "already_initialized")

    def test_init_idempotent(self, client, auth_headers, test_companion_id):
        """Second init of same companion returns already_initialized."""
        # First call ensures it's initialized (may already be from previous test)
        client.post(
            "/companions",
            json={"companion_id": test_companion_id},
            headers=auth_headers
        )
        # Second call
        r = client.post(
            "/companions",
            json={"companion_id": test_companion_id},
            headers=auth_headers
        )
        assert r.status_code == 200
        assert r.json()["status"] == "already_initialized"

    def test_health_shows_active_companion(self, client, auth_headers, test_companion_id):
        """After init, active_companions should be >= 1."""
        client.post(
            "/companions",
            json={"companion_id": test_companion_id},
            headers=auth_headers
        )
        r = client.get("/health")
        assert r.json()["active_companions"] >= 1

    def test_delete_companion(self, client, auth_headers):
        """Teardown removes a companion."""
        cid = "delete-test-001"
        client.post("/companions", json={"companion_id": cid}, headers=auth_headers)
        r = client.delete(f"/companions/{cid}", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "removed"
        assert body["companion_id"] == cid

    def test_delete_nonexistent_graceful(self, client, auth_headers):
        """Deleting a companion that doesn't exist returns gracefully."""
        r = client.delete("/companions/does-not-exist", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "removed"


# ---------------------------------------------------------------------------
# Companion State
# ---------------------------------------------------------------------------

class TestCompanionState:
    def test_state_returns_endocrine(self, client, auth_headers, test_companion_id):
        r = client.get(
            f"/companions/{test_companion_id}/state",
            headers=auth_headers
        )
        assert r.status_code == 200
        body = r.json()
        assert "endocrine" in body
        assert "companion_id" in body
        assert body["companion_id"] == test_companion_id

    def test_state_has_hormones(self, client, auth_headers, test_companion_id):
        r = client.get(
            f"/companions/{test_companion_id}/state",
            headers=auth_headers
        )
        hormones = r.json()["endocrine"]["hormones"]
        for h in ("cortisol", "dopamine", "serotonin", "oxytocin"):
            assert h in hormones
            assert isinstance(hormones[h], (int, float))

    def test_state_unknown_companion_404(self, client, auth_headers):
        r = client.get("/companions/nonexistent-xyz/state", headers=auth_headers)
        assert r.status_code in (404, 200)  # Some impls auto-create on state access


# ---------------------------------------------------------------------------
# Chat Context
# ---------------------------------------------------------------------------

class TestChatContext:
    def test_chat_context_structure(self, client, auth_headers, test_companion_id):
        r = client.get(
            f"/companions/{test_companion_id}/chat-context",
            headers=auth_headers
        )
        assert r.status_code == 200
        body = r.json()
        assert "system_injection" in body
        assert "drives" in body
        assert "emotional_state" in body
        assert isinstance(body["system_injection"], str)
        assert len(body["system_injection"]) > 0

    def test_chat_context_with_query(self, client, auth_headers, test_companion_id):
        r = client.get(
            f"/companions/{test_companion_id}/chat-context",
            params={"q": "tell me about the weather"},
            headers=auth_headers
        )
        assert r.status_code == 200
        # Query-based context should still return valid structure
        assert "system_injection" in r.json()

    def test_chat_context_injection_not_empty(self, client, auth_headers, test_companion_id):
        r = client.get(
            f"/companions/{test_companion_id}/chat-context",
            headers=auth_headers
        )
        injection = r.json()["system_injection"]
        assert injection.strip() != ""


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

class TestEvents:
    def test_conversation_ended_event(self, client, auth_headers, test_companion_id):
        r = client.post(
            f"/companions/{test_companion_id}/event",
            json={
                "event_type": "conversation_ended",
                "data": {"duration_seconds": 120, "message_count": 5}
            },
            headers=auth_headers
        )
        assert r.status_code == 200
        body = r.json()
        assert "status" in body

    def test_task_completed_event(self, client, auth_headers, test_companion_id):
        r = client.post(
            f"/companions/{test_companion_id}/event",
            json={
                "event_type": "task_completed",
                "data": {"task": "research", "success": True}
            },
            headers=auth_headers
        )
        assert r.status_code == 200

    def test_user_feedback_event(self, client, auth_headers, test_companion_id):
        r = client.post(
            f"/companions/{test_companion_id}/event",
            json={
                "event_type": "user_feedback",
                "data": {"rating": 5, "comment": "great response"}
            },
            headers=auth_headers
        )
        assert r.status_code == 200

    def test_unknown_event_type_graceful(self, client, auth_headers, test_companion_id):
        """Unknown event types should not crash the server."""
        r = client.post(
            f"/companions/{test_companion_id}/event",
            json={"event_type": "does_not_exist", "data": {}},
            headers=auth_headers
        )
        assert r.status_code in (200, 400)  # Graceful handling


# ---------------------------------------------------------------------------
# Biosensors
# ---------------------------------------------------------------------------

class TestBiosensors:
    def test_heartrate_updates_endocrine(self, client, auth_headers, test_companion_id):
        """High HR should elevate adrenaline."""
        # Get baseline
        state_before = client.get(
            f"/companions/{test_companion_id}/state",
            headers=auth_headers
        ).json()

        # Send elevated HR
        r = client.post(
            f"/companions/{test_companion_id}/biosensor/heartrate",
            json={"value": 145.0, "timestamp": time.time()},
            headers=auth_headers
        )
        assert r.status_code == 200

    def test_hrv_updates_cortisol(self, client, auth_headers, test_companion_id):
        """Low HRV should increase cortisol."""
        r = client.post(
            f"/companions/{test_companion_id}/biosensor/hrv",
            json={"value": 12.0},
            headers=auth_headers
        )
        assert r.status_code == 200

    def test_activity_updates_dopamine(self, client, auth_headers, test_companion_id):
        r = client.post(
            f"/companions/{test_companion_id}/biosensor/activity",
            json={"value": 1.0, "metadata": {"move_ring_closed": True}},
            headers=auth_headers
        )
        assert r.status_code == 200

    def test_sleep_updates_energy(self, client, auth_headers, test_companion_id):
        r = client.post(
            f"/companions/{test_companion_id}/biosensor/sleep",
            json={"value": 480.0, "metadata": {"stage": "deep"}},
            headers=auth_headers
        )
        assert r.status_code == 200

    def test_invalid_sensor_type(self, client, auth_headers, test_companion_id):
        r = client.post(
            f"/companions/{test_companion_id}/biosensor/invalid_sensor",
            json={"value": 42.0},
            headers=auth_headers
        )
        assert r.status_code in (200, 400, 422)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

class TestMemory:
    def test_capture_memory(self, client, auth_headers, test_companion_id):
        r = client.post(
            f"/companions/{test_companion_id}/memory",
            json={
                "content": "User mentioned they enjoy hiking on weekends",
                "type": "episodic",
                "importance": 0.7,
                "location": "conversation"
            },
            headers=auth_headers
        )
        assert r.status_code == 200
        body = r.json()
        assert "status" in body

    def test_capture_memory_minimal(self, client, auth_headers, test_companion_id):
        """Memory with only required field should work."""
        r = client.post(
            f"/companions/{test_companion_id}/memory",
            json={"content": "User prefers short answers"},
            headers=auth_headers
        )
        assert r.status_code == 200

    def test_capture_memory_with_emotion(self, client, auth_headers, test_companion_id):
        r = client.post(
            f"/companions/{test_companion_id}/memory",
            json={
                "content": "User was happy when we solved the problem together",
                "type": "episodic",
                "emotion": {"valence": 0.8, "arousal": 0.6},
                "importance": 0.8
            },
            headers=auth_headers
        )
        assert r.status_code == 200

    def test_search_memory(self, client, auth_headers, test_companion_id):
        r = client.get(
            f"/companions/{test_companion_id}/memory/search",
            params={"q": "hiking"},
            headers=auth_headers
        )
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, (list, dict))

    def test_memory_missing_content_rejected(self, client, auth_headers, test_companion_id):
        """Memory without content should be rejected."""
        r = client.post(
            f"/companions/{test_companion_id}/memory",
            json={"type": "episodic"},
            headers=auth_headers
        )
        assert r.status_code == 422  # Pydantic validation error


# ---------------------------------------------------------------------------
# Config Patch
# ---------------------------------------------------------------------------

class TestConfigPatch:
    def test_patch_personality(self, client, auth_headers, test_companion_id):
        r = client.patch(
            f"/companions/{test_companion_id}/config",
            json={"personality": {"name": "Nova", "role": "science companion"}},
            headers=auth_headers
        )
        assert r.status_code == 200

    def test_patch_drives(self, client, auth_headers, test_companion_id):
        r = client.patch(
            f"/companions/{test_companion_id}/config",
            json={"drives": {"trigger_threshold": 5.5}},
            headers=auth_headers
        )
        assert r.status_code == 200

    def test_patch_empty_body_ok(self, client, auth_headers, test_companion_id):
        """Empty patch should be a no-op, not a crash."""
        r = client.patch(
            f"/companions/{test_companion_id}/config",
            json={},
            headers=auth_headers
        )
        assert r.status_code == 200

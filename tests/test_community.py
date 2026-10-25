"""Canal compartido real, respuestas del modelo simuladas y cola persistente."""
import json
import time
import unittest
import uuid

import test_project as fixtures


class CommunityTest(unittest.TestCase):
    setUp = fixtures.ProjectTest.setUp
    tearDown = fixtures.ProjectTest.tearDown
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    upstream = fixtures.ProjectTest.upstream
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation
    send = fixtures.ProjectTest.send

    def post(self, headers, text="hola", identifier=None):
        return self.client.post("/api/messages", headers=headers, json={"message": text, "request_id": identifier or str(uuid.uuid4())})

    def wait_reply(self, message_id):
        for _ in range(150):
            messages = self.client.get("/api/messages").json()
            original = next(item for item in messages if item["id"] == message_id)
            if original["assistant_status"] in {"complete", "failed"}:
                return original, messages
            time.sleep(.01)
        self.fail("La respuesta simulada no terminó")

    def test_greeting_gets_a_shared_ai_reply_without_private_context_or_tools(self):
        headers = self.register()
        conversation = self.conversation(headers)
        self.send(headers, conversation, "HISTORIAL_PRIVADO_123")
        self.client.post("/api/agent/memory", headers=headers, json={"title": "PRIVADO_456", "content": "DOCUMENTO_PRIVADO_789"})
        self.requests.clear()
        response = self.post(headers, "ola")
        self.assertEqual(response.status_code, 200, response.text)
        original, messages = self.wait_reply(response.json()["message_id"])
        self.assertEqual(original["assistant_status"], "complete")
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertEqual(messages[1]["reply_to"], original["id"])
        self.assertEqual(messages[1]["reply_username"], "alice")
        body = self.requests[0][1]
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("tools", body)
        self.assertEqual(body["messages"][-1], {"role": "user", "content": "alice: ola"})
        self.assertNotIn("HISTORIAL_PRIVADO_123", json.dumps(body))
        self.assertNotIn("DOCUMENTO_PRIVADO_789", json.dumps(body))
        self.client.cookies.clear()
        self.register("bob")
        self.assertEqual(len(self.client.get("/api/messages").json()), 2)
        self.assertEqual(self.client.get("/api/agent/conversations").json(), [])

    def test_retries_of_the_same_send_do_not_duplicate_user_or_ai_messages(self):
        headers = self.register()
        identifier = str(uuid.uuid4())
        first = self.post(headers, identifier=identifier)
        self.wait_reply(first.json()["message_id"])
        second = self.post(headers, identifier=identifier)
        self.assertEqual(first.json()["message_id"], second.json()["message_id"])
        self.assertEqual(len(self.client.get("/api/messages").json()), 2)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.post(headers, "mensaje diferente", identifier).status_code, 409)

    def test_failed_model_keeps_user_message_and_exposes_a_safe_error(self):
        headers = self.register()
        self.mode = "failure"
        response = self.post(headers)
        original, messages = self.wait_reply(response.json()["message_id"])
        self.assertEqual(original["assistant_status"], "failed")
        self.assertTrue(original["assistant_error"])
        self.assertEqual(len(messages), 1)
        self.assertEqual(original["message"], "hola")
        self.assertNotIn("fixture-secret-only-on-server", json.dumps(messages))

    def test_queue_returns_before_model_finishes_and_restarts_with_one_reply(self):
        headers = self.register()
        self.mode = "slow"
        response = self.post(headers)
        self.assertEqual(response.json()["assistant_status"], "queued")
        self.assertTrue(self.entered.wait(2))
        self.client.portal.call(self.app.state.community_agent.close)
        with self.app.state.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT status FROM community_replies").fetchone()[0], "queued")
        self.mode = "text"
        self.client.portal.call(self.app.state.community_agent.start)
        original, messages = self.wait_reply(response.json()["message_id"])
        self.assertEqual(original["assistant_status"], "complete")
        self.assertEqual(len(messages), 2)

    def test_each_reply_uses_earlier_public_messages_and_not_a_later_question(self):
        headers = self.register()
        self.mode = "slow"
        first = self.post(headers, "PRIMERA_PREGUNTA").json()["message_id"]
        self.assertTrue(self.entered.wait(2))
        second = self.post(headers, "SEGUNDA_PREGUNTA").json()["message_id"]
        self.wait_reply(second)
        calls = [item[1]["messages"] for item in self.requests]
        self.assertEqual(len(calls), 2)
        self.assertNotIn("SEGUNDA_PREGUNTA", json.dumps(calls[0]))
        self.assertIn("PRIMERA_PREGUNTA", json.dumps(calls[1]))
        self.assertTrue(any(item["role"] == "assistant" for item in calls[1]))
        self.assertEqual(calls[1][-1]["content"], "alice: SEGUNDA_PREGUNTA")
        self.assertEqual(self.wait_reply(first)[0]["assistant_status"], "complete")

    def test_legacy_public_messages_are_not_replied_to_on_startup(self):
        headers = self.register()
        self.app.state.database.post_community("alice", "HISTORICO")
        self.client.portal.call(self.app.state.community_agent.close)
        self.client.portal.call(self.app.state.community_agent.start)
        messages = self.client.get("/api/messages").json()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")
        self.assertIsNone(messages[0]["assistant_status"])
        self.assertFalse(self.requests)

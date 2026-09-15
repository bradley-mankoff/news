from __future__ import annotations

import json
import unittest
from io import BytesIO
from typing import Any

from news_pipeline import ui as ui_module


def _invoke(method: str, path: str, body: str | None = None) -> tuple[int, str]:
    payload = (body or "").encode("utf-8")
    handler = object.__new__(ui_module.NewsUIHandler)
    state: dict[str, Any] = {"status": None}
    handler.path = path
    handler.headers = {"Content-Length": str(len(payload))}
    handler.rfile = BytesIO(payload)  # type: ignore[assignment]
    handler.wfile = BytesIO()  # type: ignore[assignment]
    handler.send_response = lambda status: state.__setitem__("status", status)
    handler.send_header = lambda name, value: None
    handler.end_headers = lambda: None
    getattr(handler, method)()
    return state["status"], handler.wfile.getvalue().decode("utf-8")  # type: ignore[attr-defined]


class AssistantChatPanelTests(unittest.TestCase):
    def test_help_button_opens_in_page_panel(self) -> None:
        html = ui_module.HTML
        # Help button and panel live inside the page.
        self.assertIn('id="assistantFab"', html)
        self.assertIn('id="assistantPanel"', html)
        self.assertIn('id="assistantClose"', html)
        self.assertIn('id="assistantMessages"', html)
        self.assertIn('id="assistantInput"', html)
        self.assertIn('id="assistantSend"', html)
        # Bottom-right fixed positioning, hidden until toggled.
        self.assertIn("#assistantFab { position: fixed; right: 16px; bottom: 16px;", html)
        self.assertIn(".assistant-panel { position: fixed; right: 16px; bottom: 76px;", html)
        self.assertIn('class="assistant-panel hidden"', html)
        # In-page toggle: no new window for the helper.
        self.assertIn("function toggleAssistantPanel(", html)
        panel_js = html.split("function toggleAssistantPanel(")[1].split("function appendAssistantMessage(")[0]
        self.assertIn('classList.toggle("hidden"', panel_js)
        self.assertNotIn("window.open", panel_js)
        assistant_markup = html.split('id="assistantPanel"')[1].split("</section>")[0]
        self.assertNotIn("target=", assistant_markup)

    def test_panel_follows_advanced_mode(self) -> None:
        html = ui_module.HTML
        # Hidden outside advanced mode; the DN-78 mode state drives it.
        self.assertIn('body[data-ui-mode="advanced"] #assistantFab', html)
        self.assertIn('body[data-ui-mode="advanced"] #assistantPanel.hidden', html)
        setter = html.split("function setUiMode(")[1].split("function wizardEnabled()")[0]
        self.assertIn("renderAssistantPanel();", setter)
        self.assertIn("ASSISTANT_MODEL_STORAGE_KEY", html)

    def test_model_picker_lists_configured_local_models(self) -> None:
        html = ui_module.HTML
        self.assertIn('<select id="assistantModel">', html)
        models_fn = html.split("function assistantModels()")[1].split("function renderAssistantPanel()")[0]
        self.assertIn("state.schema.model_catalog", models_fn)
        picker = html.split("function renderAssistantPanel()")[1].split("function toggleAssistantPanel(")[0]
        self.assertIn("assistantModels()", picker)
        self.assertIn("<option", picker)
        catalog = ui_module.list_model_catalog()
        self.assertTrue(catalog)
        # The default answer model is the catalog default.
        result = ui_module.assistant_chat({"message": "What does delivery mode do?"})
        default_alias = next(entry["alias"] for entry in catalog if entry.get("is_default"))
        self.assertEqual(result["model"], default_alias)
        # An explicit configured alias is honored; unknown ones fail closed.
        result = ui_module.assistant_chat(
            {"message": "What does delivery mode do?", "model": catalog[1]["alias"]}
        )
        self.assertEqual(result["model"], catalog[1]["alias"])
        with self.assertRaises(ValueError) as ctx:
            ui_module.assistant_chat({"message": "What does delivery mode do?", "model": "nope"})
        self.assertIn("Unknown assistant model", str(ctx.exception))

    def test_setting_question_names_matching_control(self) -> None:
        knob = ui_module.match_assistant_control("What does delivery mode do?")
        self.assertIsNotNone(knob)
        assert knob is not None
        self.assertEqual(knob["env"], "NEWS_DELIVERY_MODE")
        answer = ui_module.describe_assistant_control(knob)
        self.assertIn("Delivery mode", answer)
        self.assertIn("NEWS_DELIVERY_MODE", answer)
        self.assertIn("plain words", answer)
        result = ui_module.assistant_chat({"message": "What does delivery mode do?"})
        self.assertEqual(result["control"], {"label": "Delivery mode", "env": "NEWS_DELIVERY_MODE"})
        self.assertIn("NEWS_DELIVERY_MODE", result["answer"])
        self.assertFalse(result["no_model"])
        # Bare model questions resolve to the Model control, not a task model.
        model_knob = ui_module.match_assistant_control("What does the Model setting do?")
        self.assertIsNotNone(model_knob)
        assert model_knob is not None
        self.assertEqual(model_knob["env"], "NEWS_MODEL")

    def test_setting_answer_is_long_plain_words(self) -> None:
        knob = ui_module.match_assistant_control("What does delivery mode do?")
        assert knob is not None
        answer = ui_module.describe_assistant_control(knob)
        sentences = [part for part in answer.split(". ") if part.strip()]
        self.assertGreaterEqual(len(sentences), 5)
        self.assertIn("You'll find it", answer)
        self.assertIn("next preview and run", answer)
        self.assertIn("setting name is", answer)
        self.assertNotIn("`", answer)

    def test_no_model_running_message(self) -> None:
        def _down(_reference: str, _message: str) -> str:
            raise OSError("connection refused")

        result = ui_module.assistant_chat(
            {"message": "blarg nonsense xyzzy"}, model_caller=_down
        )
        self.assertTrue(result["no_model"])
        self.assertEqual(result["answer"], ui_module.ASSISTANT_NO_MODEL_MESSAGE)
        self.assertIn("No local model is running", result["answer"])
        with self.assertRaises(ValueError):
            ui_module.assistant_chat({"message": "   "})

    def test_chat_route(self) -> None:
        status, body = _invoke(
            "do_POST", "/api/assistant/chat", body=json.dumps({"message": "What does delivery mode do?"})
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("NEWS_DELIVERY_MODE", payload["answer"])
        self.assertEqual(payload["control"]["env"], "NEWS_DELIVERY_MODE")
        status, body = _invoke(
            "do_POST", "/api/assistant/chat", body=json.dumps({"message": "   "})
        )
        self.assertEqual(status, 400)
        self.assertIn("Ask a question", json.loads(body)["error"])

    def test_spoken_source_list_change_is_backend_validated(self) -> None:
        # DN-81 AC: "set source list to All" validates backend-side with no LLM.
        def _boom(_reference: str, _message: str) -> str:
            raise AssertionError("change path must not call the model")

        result = ui_module.assistant_chat(
            {"message": "set source list to All"}, model_caller=_boom
        )
        self.assertFalse(result["no_model"])
        self.assertEqual(result["control"], {"label": "Source scope", "env": "NEWS_SOURCE_SCOPE"})
        self.assertEqual(
            result["change"],
            {"env": "NEWS_SOURCE_SCOPE", "value": "peripheral", "label": "Source scope"},
        )
        self.assertIn("Source scope", result["answer"])
        self.assertIn("peripheral", result["answer"])
        # Direct parser agrees: spoken "All" normalizes to the stored value.
        parsed = ui_module.parse_assistant_setting_change("set source list to All")
        assert parsed is not None and parsed.get("knob") is not None
        self.assertEqual(parsed["knob"]["env"], "NEWS_SOURCE_SCOPE")
        self.assertEqual(parsed["value"], "peripheral")
        # Plain questions are not changes.
        self.assertIsNone(ui_module.parse_assistant_setting_change("What does delivery mode do?"))
        # Route carries the backend-validated change payload.
        status, body = _invoke(
            "do_POST", "/api/assistant/chat", body=json.dumps({"message": "set source list to All"})
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["change"]["env"], "NEWS_SOURCE_SCOPE")
        self.assertEqual(payload["change"]["value"], "peripheral")

    def test_spoken_change_applies_through_backend_route_without_clicks(self) -> None:
        html = ui_module.HTML
        sender = html.split("async function sendAssistantMessage()")[1].split("function wizardEnabled()")[0]
        # Every change travels through the chat backend route.
        self.assertIn('api("/api/assistant/chat"', sender)
        # The panel assigns the backend-validated env/value directly.
        self.assertIn("data.change", sender)
        self.assertIn("setControlValue(data.change.env, data.change.value)", sender)
        # No simulated clicks anywhere on the apply path; no page reload.
        self.assertNotIn(".click(", sender)
        self.assertNotIn("dispatchEvent", sender)
        self.assertNotIn("location.reload", sender)
        self.assertNotIn("window.location", sender)
        # No value mapping lives in the panel: "All" -> "peripheral" is backend-owned.
        self.assertNotIn("peripheral", sender)

    def test_spoken_change_fail_closed_and_other_types(self) -> None:
        # Unknown values fail closed with valid options and no change payload.
        result = ui_module.assistant_chat({"message": "set source list to banana"})
        self.assertIsNone(result["change"])
        self.assertEqual(result["control"], {"label": "Source scope", "env": "NEWS_SOURCE_SCOPE"})
        self.assertIn("core", result["answer"])
        self.assertIn("peripheral", result["answer"])
        # Boolean knobs accept spoken on/off.
        on_result = ui_module.assistant_chat({"message": "turn image generation on"})
        self.assertEqual(on_result["change"], {"env": "NEWS_IMAGE_ENABLED", "value": "1", "label": "Image generation"})
        off_result = ui_module.assistant_chat({"message": "disable image generation"})
        self.assertEqual(off_result["change"]["value"], "0")
        # Number knobs extract the spoken number.
        num_result = ui_module.assistant_chat({"message": "set max stories to 5"})
        self.assertEqual(num_result["change"], {"env": "NEWS_MAX_STORIES", "value": "5", "label": "Max stories"})


if __name__ == "__main__":
    unittest.main()

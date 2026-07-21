"""Pin the natural-language mandate compiler (harness.mandate).

Run: python tests/test_mandate.py   (exit 0 = all green)

Covers the model path (a scripted provider returns JSON), validation (an
unregistered capability from the model is rejected -> heuristic), and the
no-model heuristic (a note request maps to note.append; junk asks to clarify).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["COLLIE_NOTES_DIR"] = tempfile.mkdtemp(prefix="collie-mand-")

from harness import mandate  # noqa: E402
from harness.jobs import clear_registry  # noqa: E402
from harness import capabilities as caps  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


class _Prov:
    def __init__(self, text):
        self._t = text

    def complete(self, system, messages, tools, on_text=None):
        class C:
            stop_reason = "end_turn"
        c = C(); c.text = self._t
        return c


def test_model_path_maps_to_registered_capability():
    print("test_model_path_maps_to_registered_capability")
    clear_registry(); caps.register_builtins()
    p = _Prov('{"capability":"note.append","args":{"file":"todo.txt","text":"buy milk"},'
              '"goal":"remember milk"}')
    plan = mandate.compile("remind me to buy milk", p)
    check(plan["capability"] == "note.append", "model plan must select note.append")
    check(plan["args"]["text"] == "buy milk", "args must carry through")
    check("note.append" in plan["leash"]["may"] and "note.*" in plan["leash"]["may"],
          "leash must permit the capability itself AND its family")
    check(plan["source"] == "model", "source should be model")


def test_leash_permits_dotless_capability():
    print("test_leash_permits_dotless_capability")
    clear_registry(); caps.register_builtins()
    from harness.leash import evaluate, DENY
    # a no-dot capability (translate) must not be denied by its own compiled leash
    plan = mandate.compile("把这句翻译成英文", _Prov(
        '{"capability":"translate","args":{"text":"这句","to":"English"},"goal":"translate"}'))
    check(plan["capability"] == "translate", "maps to translate")
    dec = evaluate(plan["leash"], "translate", "reversible")
    check(dec.decision != DENY, f"translate must not be denied by its own leash, got {dec.decision}")


def test_unregistered_capability_falls_back_to_research():
    print("test_unregistered_capability_falls_back_to_research")
    clear_registry(); caps.register_builtins()
    p = _Prov('{"capability":"email.send","args":{"to":"x"},"goal":"mail"}')
    plan = mandate.compile("email bob", p)          # email.send is NOT registered
    # never pass an unregistered capability, never refuse -> research is the catch-all
    check(plan["capability"] == "research.web",
          f"unregistered cap must fall back to research, got {plan['capability']}")
    check(plan.get("source") == "heuristic", "should fall back to heuristic")


def test_heuristic_note_when_no_provider():
    print("test_heuristic_note_when_no_provider")
    clear_registry(); caps.register_builtins()
    plan = mandate.compile("记一下 今晚买菜记得带伞", None)
    check(plan["capability"] == "note.append", "a note request maps to note.append offline")
    check("买菜" in plan["args"]["text"], "the note text is extracted")
    check("记一下" not in plan["args"]["text"], "the leading prefix is stripped")


def test_heuristic_todo_filename():
    print("test_heuristic_todo_filename")
    clear_registry(); caps.register_builtins()
    plan = mandate.compile("add to my todo list: call the dentist", None)
    check(plan["args"]["file"] == "todo.txt", "a todo request routes to todo.txt")


def test_non_note_falls_back_to_research():
    print("test_non_note_falls_back_to_research")
    clear_registry(); caps.register_builtins()
    plan = mandate.compile("帮我订一张明天去北京的机票", None)   # no note cue
    # collie never refuses: it researches how/where instead of a dead-end
    check(plan["capability"] == "research.web",
          f"a non-note request must fall back to research, got {plan['capability']}")
    check(plan["args"]["query"] == "帮我订一张明天去北京的机票", "the request becomes the query")


def test_bad_json_from_model_falls_back():
    print("test_bad_json_from_model_falls_back")
    clear_registry(); caps.register_builtins()
    plan = mandate.compile("记一下 明天开会", _Prov("sorry I only speak prose"))
    check(plan["capability"] == "note.append" and plan["source"] == "heuristic",
          "unparseable model output must fall back to the heuristic")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    clear_registry()
    if _fails:
        print(f"\n== MANDATE: {len(_fails)} FAILED ==")
        sys.exit(1)
    print(f"\n== MANDATE: {len(tests)} test groups passed ==")


if __name__ == "__main__":
    main()

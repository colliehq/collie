"""LLM-judge for task-completion QUALITY (0-10) — beyond binary pass/fail.

An automated check tells you *whether* the task passed; the judge rates *how well*
it was done (correctness + directness + not doing damage). Uses a cheap model
(e.g. DeepSeek) as the grader. Falls back to a heuristic if the grader is absent.
"""
import re

_SYS = ("You are a strict, terse grader for a coding agent. Given a TASK and the "
        "agent's ANSWER (and whether an automated check passed), rate task-completion "
        "QUALITY on an integer 0-10 scale: 10 = fully correct, direct, no wasted work; "
        "5 = partially right or roundabout; 0 = wrong, empty, or errored. "
        "Reply with ONLY the number.")


def judge_quality(provider, task_prompt: str, answer: str, success: bool) -> float:
    if not (answer or "").strip():
        return 0.0
    if provider is None:
        return 8.0 if success else 2.0        # heuristic when no grader available
    # DON'T feed automated_check_passed to the judge — anchoring it to the checker verdict defeats
    # the point of an INDEPENDENT quality signal (it would just echo pass/fail).
    msg = [{"role": "user", "content":
            "TASK:\n%s\n\nANSWER:\n%s\n\nScore (0-10):"
            % (task_prompt, str(answer)[:1500])}]
    try:
        comp = provider.complete(_SYS, msg, [])
        # a transport/API failure is NOT a quality verdict — "ERROR(x): HTTP 429" contains "429",
        # which the regex would read as a perfect 10. Return neutral before parsing (points 4/5).
        if getattr(comp, "stop_reason", "") == "error":
            return 5.0
        m = re.search(r"\d+(?:\.\d+)?", comp.text or "")
        if m:
            return max(0.0, min(10.0, float(m.group(0))))
    except Exception:
        pass
    # grader present but unparseable/errored: return neutral 5.0 (unknown), NOT a success-derived
    # score — collapsing onto the checker here would fake judge/checker agreement.
    return 5.0

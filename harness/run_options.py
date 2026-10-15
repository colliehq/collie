"""Pure request option normalization, independent of application frontends."""


def normalize_run_options(intent="build", quality="balanced", verification="auto"):
    intent = str(intent or "build").strip().lower()
    quality = str(quality or "balanced").strip().lower()
    verification = str(verification or "auto").strip().lower()
    if intent not in ("build", "plan", "test", "review"):
        raise ValueError("intent must be build, plan, test, or review")
    if quality not in ("quick", "balanced", "thorough"):
        raise ValueError("quality must be quick, balanced, or thorough")
    if verification not in ("auto", "required"):
        raise ValueError("verification must be auto or required")
    return {"intent": intent, "quality": quality, "verification": verification}

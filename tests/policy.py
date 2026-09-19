"""A scripted System One model for the tests: answers every question the compiler asks."""


def choice(questions, name, pick, p=0.95):
    crit = questions[name]["criteria"]
    probs = {k: 0.0 for k in crit}
    probs[pick] = p
    return {"type": "choice", "choice": pick, "probabilities": probs, "confidence": p}


def fill(questions, answers):
    """Every question gets an answer; unspecified ones a middling one."""
    for k, q in questions.items():
        if k in answers:
            continue
        if q["type"] == "choice":
            answers[k] = choice(questions, k, next(iter(q["criteria"])), 0.5)
        elif q["type"] == "noul":
            answers[k] = {"type": "noul", "noul": 0.5}
        else:
            answers[k] = {"type": "score", "score": 0, "probabilities": {}, "confidence": 0.5}
    return answers


def perfect(state, questions):
    """The policy that ships an order: pick everything, pack, choose post, ship."""
    obs = state["observation"]
    a = {"goal_reached": {"type": "noul", "noul": 0.02}}
    if obs.get("unpicked"):
        a["next_action"] = choice(questions, "next_action", "pick_item")
        a["pick_item__item"] = choice(questions, "pick_item__item", obs["unpicked"][0])
    elif not obs.get("packed"):
        a["next_action"] = choice(questions, "next_action", "pack")
    elif not obs.get("carrier"):
        a["next_action"] = choice(questions, "next_action", "choose_carrier")
        a["choose_carrier__carrier"] = choice(questions, "choose_carrier__carrier", "post")
    else:
        a["next_action"] = choice(questions, "next_action", "ship")
    return fill(questions, a)

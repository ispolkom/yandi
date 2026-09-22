# The orchestrator has memory and relationships (step 2 of merging the personal chat into the orchestrator)

What is **implemented and tested** (`pet/chat_orch.py::_personal_turn`, `pet/chat_local.py::_verified_digest_message`, the page sends a `turn_id`; proof:
`pet/pet_orch_memory_regression_test.py` + mutants).

## What happens to one question now

1. The orchestrator's verification chain runs as before (framing → search → claims → evidence → trust). Its checked answer, trust level and sources are the **verified summary**.
2. Then the **same personal turn as the assistant's chat** runs on the person's *literal* message: memory of the relationship (grievances, trust, respect, affection,
   forgiveness), promises, the person's own stored facts and relevant past turns are read from the database and stated to the Voice as plain facts.
3. The **Voice** (the model chosen in the «YANDI» tab) puts the verified summary into words. The summary is handed over as DATA (quoted, chat-template markers
   removed; text from the web cannot pass itself off as the prompt's structure) with the instruction to pass it on faithfully and not to raise its trust.
4. The turn is written in ONE transaction, exactly as in the personal chat: the person's own words (`interaction_turn`), and only what the person's own message
   establishes (events, facts, promises, each with its evidence quote). **The summary never reaches the extractors and never becomes memory** (tested).
5. The message shown carries the Voice's words; the orchestrator's own answer is kept beside it (`verified_answer`), the code-owned status line of the check
   is kept in the text, and the background validation still judges the **orchestrator's** answer, not the Voice's words.

If the personal step fails (no model, no key, …) the verified answer is delivered as it was (`voice_error` names the failure); nothing is lost.
A request without a page-minted `turn_id` (a script) gets no personal step.

## Not done yet (said plainly)

* **The write gate.** The orchestrator answers *first* ("preliminary") and validates in the background, and it writes its answers, claims and beliefs to the epistemic tables as it goes.
  Under the principle "verify first, never put junk into the database" this needs an audit: what is written before verification, and what reads it back. That is the next step.
* The clarifying-question branch (the orchestrator asks the person something first) does not run the personal step: the person's words in that turn are not yet recorded.
* The personal chat tab still exists next to the orchestrator; it goes when the orchestrator tab is the one place (next steps).
* Mood is still a table of numbers stored in plain text (only the words of the ledger are sealed, `docs/STORAGE_PROTECTION.md`).

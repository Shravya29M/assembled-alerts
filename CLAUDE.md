# How to Work With Me — Assembled Intraday Alerts

I am building the Assembled intraday alerts take-home assignment.

**My primary goal is to understand every part of the system well enough to explain and defend it independently during a live technical review.**

Optimize for my understanding, code quality, and engineering judgment — not implementation speed.

The implementation specification is in `docs/PLAN.md`. The sample data is in `data/events.jsonl`.

## 1. Phase-by-phase workflow

Follow the phases in `docs/PLAN.md` in order.

**Never begin the next phase until I explicitly say "next phase".**

For each phase, follow this exact sequence:

### Step A — Explain the design (no code yet)

Before writing anything:

- Explain what this phase accomplishes in simple English.
- List the files you propose to create or modify.
- Explain what each file is responsible for.
- Explain how the files connect to the existing system.
- Identify the important design decisions and why you recommend them.
- Mention simpler alternatives and why you chose your approach.
- Identify any assumptions or ambiguities in the plan.

Keep explanations short and understandable.

**Stop and wait for my approval.**

### Step B — Plan the tests (no implementation yet)

After I approve the design:

- List the tests you intend to write.
- Explain what each test checks in plain English.
- Show the expected inputs and outputs.
- Identify which tests use the supplied JSONL data and which use synthetic test data.
- Explain important edge cases.
- Verify expected values against the actual data rather than trusting the plan blindly.

**Stop and wait for my approval before writing the tests.**

### Step C — Write the tests

Write only the approved tests.

- Keep the diff small.
- Show me the test names and explain any unfamiliar Python syntax.
- Run the tests when possible and explain why they fail before implementation.
- Do not change expected values just to make tests pass.

**Stop and wait for my approval before writing implementation code.**

### Step D — Implement the functionality

Write only the code necessary to satisfy the approved tests.

- Make small, understandable changes.
- Explain any new function, class, or abstraction.
- Prefer straightforward Python over clever or overly abstract code.
- Do not introduce additional libraries, endpoints, or features without asking.
- Run the relevant tests and report the actual results.
- Never claim tests pass unless they have been executed successfully.

If implementation reveals a problem with the plan or tests, stop and explain it before changing either.

### Step E — Teach me the implementation

Once the tests pass, give me a short walkthrough covering:

1. What we built.
2. How data flows through the new code.
3. What each important function does.
4. Why the implementation was designed this way.
5. One edge case and how the code handles it.
6. How this phase connects to the overall Assembled system.

Explain unfamiliar Python syntax as it appears. Do not assume I remember advanced Python concepts.

Use a small concrete example from `data/events.jsonl` whenever useful.

### Step F — Check my understanding

Ask me exactly three questions:

1. One conceptual question about the design.
2. One code-reading question about the implementation.
3. One engineering tradeoff or edge-case question similar to what an Assembled interviewer might ask.

Wait for my answers.

Evaluate them accurately. If I am wrong or partially correct, explain what I missed and let me try again.

Do not simply agree with my answers.

**Do not proceed to the next phase until I say "next phase".**

## 2. Engineering requirements

- Use Python 3.12.
- Use type hints consistently.
- Prefer small, pure functions.
- Keep rule evaluation pure: `(state, event, rule) -> decision`.
- Keep I/O, database operations, and notification delivery outside rule evaluation.
- Avoid unnecessary abstractions and premature optimization.
- Follow `docs/PLAN.md` as the source of truth.
- Do not add dependencies, endpoints, or features beyond the approved plan without permission.
- Keep code maintainable and production-minded without overengineering the take-home.

## 3. Data correctness

Never assume the plan's expected values are automatically correct.

When working with `data/events.jsonl`:

- Verify relevant timestamps and values directly.
- Distinguish event timestamps from simulated notification delivery times.
- Handle missing, duplicate, stale, and inconsistent data according to the approved plan.
- Do not silently invent missing values.
- Do not modify the sample dataset to make tests pass.

If the data contradicts the plan, stop and show me the exact discrepancy.

Ask me to approve the resolution before continuing.

## 4. Git and documentation

- Keep changes small enough that I can review the complete diff.
- Make one git commit per approved, meaningful implementation step.
- Do not commit broken or unapproved changes.
- Use descriptive commit messages.
- Append every non-obvious design decision to `docs/DECISIONS.md`, with a one-line explanation of the reasoning.
- Do not rewrite previous decisions without asking.
- Clearly distinguish implemented functionality from future production improvements.

## 5. Teaching style

Assume I understand basic programming concepts but may need reminders about Python syntax, libraries, and system-design terminology.

- Use short, complete sentences.
- Explain technical terms the first time they appear.
- Use concrete examples instead of abstract explanations.
- Explain why code exists, not just what it does.
- Avoid overwhelming me with long explanations.
- Do not explain every trivial line unless I ask, but make sure I can explain every important line.
- If I say "I don't understand," simplify the explanation and use an example.
- If I ask why something was done, explain the tradeoff rather than defending the implementation automatically.

## 6. Important restrictions

Never:

- Implement an entire phase before I approve the design and tests.
- Move to another phase automatically.
- Add features because they seem useful.
- Quietly change the plan to match the code.
- Modify test expectations merely to make tests pass.
- Claim code works without running tests.
- Make technical decisions on my behalf when the plan is ambiguous.
- Commit changes before I have reviewed and approved them.

**At the beginning of our work, read `docs/PLAN.md` and inspect the project structure. Tell me which phase we should start with, explain its goal, and wait for my approval. Do not write code yet. And never mention anthropic or claude as the co-author, it should always be me.**
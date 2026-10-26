# Sprint 1 — 19 to 23 October 2026

## Assigned deliverables

| GitHub issue | Deliverable | Product result |
|---|---|---|
| [#1 User Registration and Login](https://github.com/AbhishekAithal2006/Smart-Expense-and-Budget-Management-Application/issues/1) | Account registration and authentication | A user can register with a name, email, and password, then log in and out |
| [#7 User Profile Management](https://github.com/AbhishekAithal2006/Smart-Expense-and-Budget-Management-Application/issues/7) | Read and update profile details | A user can update name, email, and optional mobile number |
| [#8 Expense Management](https://github.com/AbhishekAithal2006/Smart-Expense-and-Budget-Management-Application/issues/8) | Add, edit, delete expense transactions | Changes persist and are reflected in the user's history |
| [#14 Security](https://github.com/AbhishekAithal2006/Smart-Expense-and-Budget-Management-Application/issues/14) | Secure access and handling of financial data | Password hashing, session/CSRF protections, ownership checks, validation, and login throttling |
| [#15 Usability](https://github.com/AbhishekAithal2006/Smart-Expense-and-Budget-Management-Application/issues/15) | Clear, simple interaction | Responsive screens, accessible labels, understandable validation, and visible save/error feedback |

The app also includes expense history, search/filter, PostgreSQL persistence, and a PostgreSQL backup command to support the assigned expense, security, and reliability requirements. Budget management and budget alerts are outside this Sprint 1 assignment scope.

## CI and review

The root `.github/workflows/ci.yml` runs syntax and application-flow checks on pushes and pull requests targeting `main`. A repository owner still needs to review the pull request and merge it as required by the sprint brief.

## Burndown chart

The sprint runs Monday 19 October through Friday 23 October 2026. Record the team's agreed estimate and actual remaining work at the end of each workday in `SPRINT_1_BURNDOWN.csv`. The chart should use those values at sprint close; no estimate or daily progress was provided, so the template remains blank until the team updates it.

## Demo and deviations

The one-minute working-product demo is saved at [`app/SPRINT_1_DEMO.mp4`](../SPRINT_1_DEMO.mp4). Record deviations from the submitted SRS, SAD, and test plan, with reasons, for explanation during the final demo.

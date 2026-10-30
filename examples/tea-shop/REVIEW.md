# What failure looks like here

For the reviewer. Read it before each review; the merge gate merges what you pass.

- **Prices, discounts, checkout and payments** (`src/lib/pricing*`, `src/checkout/`): any change is level 2. Even a
  correct one gets `review` ok with needs_human, unless an owner asked for exactly this in the conversation.
- **What customers read**: copy on product pages is level 1; anything promising a delivery date, a refund or a price
  is level 2.
- **Tests that pass by changing the test**: a PR that edits an assertion to match new behavior must say why the old
  behavior was wrong.
- **A fix of a fix**: if this PR fixes something merged in the last day, the author says why the first PR's checks
  didn't catch it and adds the test that would have.
- Not a finding: style the linter accepts, naming you'd have chosen differently, code that was already there.

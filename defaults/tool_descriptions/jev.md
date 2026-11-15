Ask Jev, TypeSafe's judgment model, typed questions about a state and get
calibrated numbers back. One call sends the state and every question in one
request.

- `noul` (the default) answers a yes/no question with a 0-1 score.
- `choice` picks one of `options` (`name` or `name=description`) and gives a
  probability for each.
- `score` places the state on `options` given as levels, lowest first.

Name a field of a JSON state in the question with backticks: "Is `claim`
supported by `evidence`?". Jev leans toward the first option of a choice;
`check_order=true` asks each choice again with its options reversed and says
whether the answer flipped. A flipped answer is not to be trusted.

Each answer comes back as one numbered line in question order. Jev needs
TYPESAFE_API_KEY in the environment.

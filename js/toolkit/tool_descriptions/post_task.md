Put a piece of work on the swarm's board for whoever takes it. Everyone on the
bus gets a `task` message with the number and the title, so an idle agent
wakes to it. The board keeps it until someone finishes it.

Write the `title` as one line of what needs doing and the `body` as what the
taker needs: where the files are, what done looks like, what to send back and
to whom. Post work you will not do yourself; take it with `take_task` if you
will. `tasks` shows the board.

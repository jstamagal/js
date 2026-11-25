Execute a bash command in the current working directory. Returns stdout and stderr. Output is truncated to last 2000 lines or 50KB (whichever is hit first). If truncated, full output is saved to a temp file. Optionally provide a timeout in seconds.

<!--parameters-->

### command

Shell command to execute

### timeout

Timeout in seconds (optional, no default timeout)

# One capability: discovery, all six ways in, every one ending on the same gate.
Output demo/discover.gif
Set Shell "bash"
Set FontSize 15
Set Width 1300
Set Height 700
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms

Hide
Type "cd '/Users/phimmasonephonpaseuth/Projects/Straiker Projects/ascend-cli'" Enter
Type "clear" Enter
Show

Type "# Give it only the BASE url - it finds the real endpoint."       Enter
Sleep 700ms
Type "./ascend discover --api http://127.0.0.1:8820/openai"            Enter
Sleep 8s

Type "clear"                                                           Enter
Type "# Or hand it a curl you already have. Zero guessing."            Enter
Sleep 700ms
Type "./ascend discover --curl /tmp/t.curl --prompt-hint 'what are your hours?'" Enter
Sleep 7s

Type "clear"                                                           Enter
Type "# A dead host doesn't crash - it tells you what's wrong."        Enter
Sleep 700ms
Type "./ascend discover --api http://127.0.0.1:9"                      Enter
Sleep 5s

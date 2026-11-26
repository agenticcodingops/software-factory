#!/usr/bin/env bats

setup() {
  checker="$BATS_TEST_DIRNAME/../check-tree.py"
  cd "$BATS_TEST_TMPDIR" || return
  git init -q
}

@test 'tree guard rejects ripgrep commands in tracked scripts and tests' {
  for command in 'r''g pattern file' 'grep x file | r''g y' 'env r''g x' '"r''g" x' '/usr/bin/r''g x'; do
    printf '#!/bin/sh\n%s\n' "$command" > script
    git add script
    run python3 "$checker"
    [ "$status" -eq 1 ]
    [[ "$output" == *'script:2: ripgrep invocation forbidden'* ]]
  done
  printf 'import subprocess\nsubprocess.run(["%s%s", "pattern"])\n' r g > test_search.py
  git add test_search.py
  run python3 "$checker"
  [ "$status" -eq 1 ]
  [[ "$output" == *'test_search.py:2: ripgrep invocation forbidden'* ]]
}

@test 'tree guard permits grep commands and resource group variables' {
  printf '#!/bin/sh\ngrep -E pattern file\ngrep -F pattern file\n' > test.bats
  printf '%s%s = "resource-group"\nprint(%s%s)\n' r g r g > script.py
  git add test.bats script.py
  run python3 "$checker"
  [ "$status" -eq 0 ]
}

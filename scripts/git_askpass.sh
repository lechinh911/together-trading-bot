#!/bin/sh
case "$1" in
 Username*) echo "x-access-token";;
 *) printf '%s\n' "$GITHUB_TOKEN_TOGETHER";;
esac

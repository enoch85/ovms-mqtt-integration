#!/bin/bash

# Pull latest changes
git pull

# Fix whitespace
find "../custom_components/ovms" -name "*.py" -exec sed -i 's/[ \t]*$//' {} \;

# Commit and push!
git commit -a -s -m "fix whitespace"
git push origin fix-lint


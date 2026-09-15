#!/usr/bin/env -S uv run --project aws --frozen python

from reaver_project_aws.cost_controls import main

if __name__ == "__main__":
    main()

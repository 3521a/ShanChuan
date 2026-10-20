name: fng-alert

on:
  schedule:
    - cron: "0 1 * * *"
  workflow_dispatch:
    inputs:
      force:
        description: "手动测试：强制发送邮件"
        type: boolean
        required: true
        default: false

permissions:
  contents: read

concurrency:
  group: fng-alert
  cancel-in-progress: false

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 10

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install requests matplotlib

      - name: Run alert
        env:
          FORCE_SEND: ${{ github.event_name == 'workflow_dispatch' && inputs.force && 'true' || 'false' }}
          MAIL_USER: ${{ secrets.MAIL_USER }}
          MAIL_PASS: ${{ secrets.MAIL_PASS }}
          MAIL_TO: ${{ secrets.MAIL_TO }}
          SMTP_HOST: smtp.gmail.com
          SMTP_PORT: "465"
          PYTHONUNBUFFERED: "1"
        run: python -u fng_alert.py

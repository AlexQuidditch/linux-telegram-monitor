#!/bin/bash

# Installing python3 and virtualenv.
# They are usually already installed in Ubuntu 20.04 and above and this command will do nothing.
sudo apt-get install -y python3 python3-virtualenv

# Setting up the python virtual environment.
# Third-party python libraries will be installed into it to not modify the global python env.
if [ -d venv ]; then
  echo "Not creating python virtualenv because venv dir exists."
else
  virtualenv -p /usr/bin/python3
  source venv/bin/activate
  pip install -r requirements.txt
fi

# Copy empty config. Please fill it after setup.
if [ -f .env ]; then
  echo "Not copying .env example config because .env file exists."
else
  cp .env.example .env
fi


# Creating the systemd service.
# This fills the current directory into the service file template and copies it to /etc/systemd/.
# The python script will be started automatically and restarted in case of failure.
cur_dir=$(pwd) envsubst < linux-telegram-monitor.service.template | sudo tee /etc/systemd/system/linux-telegram-monitor.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl restart linux-telegram-monitor

sudo systemctl status linux-telegram-monitor

echo "Please modify .env config and use \"sudo systemctl restart linux-telegram-monitor\" to restart the service."
echo "Check /var/log/syslog for the log messages."

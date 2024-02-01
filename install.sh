# It will configure to automatically run the Auto Power On and Off script using Instance Principal permission
# So ensure you have configured a dynamic group for this instance and that that dynamic group
# has a policy to manage all resources in your tenancy.

# Set to your time zone for correct time
sudo timedatectl set-timezone Europe/Amsterdam

# Install needed components and configure crontab with correct schedule
sudo pip3 install oci oci-cli
#cd OCI-PowerOn_PowerOff/
sed -i 's/UseInstancePrinciple = False/UseInstancePrinciple = True/g' Auto_PowerOn_PowerOff_Script.py
crontab schedule.cron

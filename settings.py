#!/usr/bin/python3

import cgi
import cgitb; cgitb.enable()  # for troubleshooting
import configparser
import socket
import os
import datetime

config = configparser.ConfigParser()
clientsocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
form = cgi.FieldStorage()
config_file = "/var/www/html/cgi-bin/sprinkler.config"
now = datetime.datetime.now()

def notify():
    clientsocket.connect(('localhost', 5555))
    clientsocket.send(b'config_updated')
    clientsocket.close()

def create_default():
    try:
        cfgfile = open(config_file,'w')

        # add the settings to the structure of the file, and lets write it out...
        config.add_section('forecastio')
        config.set('forecastio','apikey','key')
        config.set('forecastio','lat','37.774929')
        config.set('forecastio','lng','-122.419416')
        config.write(cfgfile)
        cfgfile.close()
        print ("New config file successfully created!")
        notify()
    except:
        print ("Config file could not be created.")

if not config.read(config_file):
    # lets create that config file for next time...
    create_default()

print("Content-type: text/html\n\n")
print("""
<html>
<head>
<title>Pi Sprinkler - Settings</title>
<style>
table, th, td {
    border: 1px solid black;
    border-collapse: collapse;
}
</style>
</head>
<body>
  <h3>Menu</h3>
<p>
<table style="">
<tr><th><a href="/">Home</a></th><th><a href="/cgi-bin/program.py">Program</a></th><th><a href="/cgi-bin/delay.py">Delay</a></th><th><a href="/cgi-bin/manual.py">Manual Control</a><th><a href="/cgi-bin/settings.py">Settings</a></th></tr>
</table>
""")

if form.getfirst("submit","") == "Change Settings":
    print("""<h3>Change Settings</h3><div style="color:red">Warning: Changing these settings could render the system useless!</div></p>""")
    print("""<form action="/cgi-bin/settings.py" method="get">""")
    for section_name in config.sections():
        print("<p><strong>Section: </strong>%s<br>" % section_name)
        for name, value in config.items(section_name):
            print("""%s = <input type="text" name="%s:%s" value="%s"><br>""" % (name, section_name, name, value))
    print("""
    </p><p>
    <input type="submit" name="submit" value="Update Settings"><input type="submit" name="submit" value="Default Settings"><input type="submit" name="submit" value="Cancel">
    </p></form>
    """)
elif form.getfirst("submit","") == "Update Settings":
    for key, value in form.items():
        if ":" in key:
            (section, option) = key.split(":", 1)
            if config.has_section(section) and config.has_option(section, option):
                config.set(section, option, value)
    with open(config_file, 'w') as cfgfile:
        config.write(cfgfile)
    notify()
    print("""<h3>Settings Updated</h3><p style="color:green">Settings have been saved successfully.</p>""")
elif form.getfirst("submit","") == "Default Settings":
    print("""
    <h3>Revert To Default Settings</h3>
    <div style="color:red">Warning: Are you sure you want to change all the settings to the defaults?</div>
    </p>
    <form action="/cgi-bin/settings.py" method="get">
    <input type="submit" name="submit" value="OK"><input type="submit" name="submit" value="Cancel">
    </form>
    """)
elif form.getfirst("submit","") == "OK":
    backup = "%s.%s" % (config_file, now.strftime("%Y%m%d_%H%M%S"))
    if os.path.exists(config_file):
        os.rename(config_file, backup)
    create_default()
    print("""
    <h3>Revert To Default Settings</h3>
    <div style="color:red">Your settings have been reverted to the defaults.</div>
    </p>
    """)
else:
    print("<h3>Current Settings</h3></p>")
    for section_name in config.sections():
        print("<p><strong>Section: </strong>%s<br>" % section_name)
        for name, value in config.items(section_name):
            print(" %s = %s<br>" % (name, value))
    print("""
    </p><p>
    <form action="/cgi-bin/settings.py" method="get">
    <input type="submit" name="submit" value="Change Settings">
    </form>
    """)

print("""
</body>
</html>
""")

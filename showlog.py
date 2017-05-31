#!/usr/bin/python

import cgi
import cgitb; cgitb.enable()  # for troubleshooting
import socket
import time
import datetime

# Create instance of FieldStorage
form = cgi.FieldStorage()
error = False

print "Content-type: text/html\n\n"
print """
<html>
<head>
<meta charset="utf-8">
<title>Pi Sprinkler - Log</title>
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
<tr><th><a href="/">Home</a></th><th><a href="/cgi-bin/program.py">Program</a></th><th><a href="/cgi-bin/delay.py">Delay</a></th><th><a href="/cgi-bin/manual.py">Manual Control</a><th><a href="/cgi-bin/settings.py">Settings</a></th><th><a href="/cgi-bin/showlog.py">Log</a></th></tr>
</table>
<p>
"""

f = open("/home/httpd/log/sprinkler-auto.log", "r")
for line in f:
  if '<' in line:
    if 'Master' in line:
      color="blue"
    else:
      color="green"
  else:
    color="black"
    
  print """<p style="color:%s">%s</p>""" % (color, line.replace('<', '&lt;'))


print """
</p></body>
</html>
"""

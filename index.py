#!/usr/bin/python3

import cgi
import cgitb; cgitb.enable()  # for troubleshooting
import configparser
import datetime
import urllib.request
import json
import socket
import time

config = configparser.ConfigParser()
today = datetime.datetime.today()
yesterday = today - datetime.timedelta(days=1)
url = "https://api.open-meteo.com/v1/forecast"

clientsocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
clientsocket.connect(('localhost', 5555))
clientsocket.send(b"status:0")
while True:
    data = clientsocket.recv(64).decode('utf-8')
    if "disabled" in data:
        (data,futuretime) = data.split(":")
        localtime = time.asctime( time.localtime(float(futuretime)) )
    break
clientsocket.close()

print("Content-type: text/html\n\n")
print("""
<html>
<head>
<title>Pi Sprinkler - Home</title>
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
<p>Current status: %s</p>
<p>
""" % data)

if not config.read("/var/www/html/cgi-bin/sprinkler.config"):
    # lets create that config file for next time...
    try:
        cfgfile = open("/var/www/html/cgi-bin/sprinkler.config",'w')

        # add the settings to the structure of the file, and lets write it out...
        config.add_section('forecastio')
        config.set('forecastio','lat','37.774929')
        config.set('forecastio','lng','-122.419416')
        config.write(cfgfile)
        cfgfile.close()
        print("""New config file successfully created! Edit the settings to set your latitude and longitude.""")
    except:
        print("Config file could not be created for first run.")

lat = config.get("forecastio","lat")
lng = config.get("forecastio","lng")

# Open-Meteo: free, no API key required
# past_days=1 includes yesterday; forecast_days=1 gives today only
params = (
    "?latitude=%s&longitude=%s"
    "&daily=temperature_2m_max,precipitation_sum"
    "&current_weather=true"
    "&temperature_unit=celsius"
    "&precipitation_unit=mm"
    "&past_days=1&forecast_days=1"
    "&timezone=auto"
) % (lat, lng)
req = urllib.request.Request(url + params)
response = urllib.request.urlopen(req)
parsed = json.loads(response.read().decode('utf-8'))

current_weather  = parsed["current_weather"]
# daily arrays: index 0 = yesterday, index 1 = today
temp_max_today     = parsed["daily"]["temperature_2m_max"][1]
temp_max_yesterday = parsed["daily"]["temperature_2m_max"][0]
precip_today       = parsed["daily"]["precipitation_sum"][1]
precip_yesterday   = parsed["daily"]["precipitation_sum"][0]

# Map WMO weather codes to human-readable descriptions
WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}
conditions = WMO_CODES.get(current_weather.get("weathercode", -1), "Unknown")

def get_precip(mm):
    """Classify daily precipitation in millimeters."""
    if mm is None:
        return "Unknown"
    if mm == 0:
        return "None"
    elif mm < 2.5:
        return "Very Light"
    elif mm < 10:
        return "Light"
    elif mm < 25:
        return "Moderate"
    else:
        return "Heavy"

print("<h3>Weather Report</h3>")
print("Current Time: %s<br>" % today)
print("Current Conditions: %s<br>" % conditions)
print("Current Temperature: %s&deg;C<br>" % current_weather["temperature"])
print("Today's Forecasted High Temperature: %s&deg;C<br>" % temp_max_today)
print("Yesterday's High Temperature: %s&deg;C<br>" % temp_max_yesterday)
print("Today's Precipitation: %s<br>" % get_precip(precip_today))
print("Yesterday's Precipitation: %s<br>" % get_precip(precip_yesterday))
print("""
</p></body>
</html>
""")

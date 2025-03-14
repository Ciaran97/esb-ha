import logging
import asyncio
from datetime import timedelta, datetime
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
import requests
import csv
import re
import json
from bs4 import BeautifulSoup
from io import StringIO
from abc import abstractmethod


from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

LOGGER = logging.getLogger(__name__)

MIN_TIME_BETWEEN_UPDATES = timedelta(hours=12)

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the ESB Smart Meter sensor."""
    pass

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the ESB Smart Meter sensor based on a config entry."""
    username = entry.data["username"]
    password = entry.data["password"]
    mprn = entry.data["mprn"]

    session = async_get_clientsession(hass)
    esb_api = ESBCachingApi(ESBDataApi(hass=hass,
                                       session=session,
                                       username=username,
                                       password=password,
                                       mprn=mprn))

    async_add_entities([
        TodaySensor(esb_api=esb_api, name='ESB Electricity Usage: Today'),
        Last24HoursSensor(esb_api=esb_api, name='ESB Electricity Usage: Last 24 Hours'),
        ThisWeekSensor(esb_api=esb_api, name='ESB Electricity Usage: This Week'),
        Last7DaysSensor(esb_api=esb_api, name='ESB Electricity Usage: Last 7 Days'),
        ThisMonthSensor(esb_api=esb_api, name='ESB Electricity Usage: This Month'),
        Last30DaysSensor(esb_api=esb_api, name='ESB Electricity Usage: Last 30 Days')
    ], True)


class BaseSensor(Entity):
    def __init__(self, *, esb_api, name):
        self._name = name
        self._state = None
        self._esb_api = esb_api

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self):
        return UnitOfEnergy.KILO_WATT_HOUR
    
    @abstractmethod
    def _get_data(self, *, esb_data):
        pass

    async def async_update(self):
        self._state = self._get_data(esb_data=await self._esb_api.fetch())
    
class TodaySensor(BaseSensor):
    def _get_data(self, *, esb_data):
        return esb_data.today


class Last24HoursSensor(BaseSensor):
    def _get_data(self, *, esb_data):
        return esb_data.last_24_hours


class ThisWeekSensor(BaseSensor):
    def _get_data(self, *, esb_data):
        return esb_data.this_week


class Last7DaysSensor(BaseSensor):
    def _get_data(self, *, esb_data):
        return esb_data.last_7_days


class ThisMonthSensor(BaseSensor):
    def _get_data(self, *, esb_data):
        return esb_data.this_month


class Last30DaysSensor(BaseSensor):
    def _get_data(self, *, esb_data):
        return esb_data.last_30_days


class ESBData:
    """Class to manipulate data retrieved from ESB"""
    
    def __init__(self, *, data):
        self._data = data
    
    def __get_data_since(self, *, since):
        return [row
                for row
                in self._data
                if datetime.strptime(row['Read Date and End Time'], '%d-%m-%Y %H:%M') >= since]
    
    def __sum_data_since(self, *, since):
        return sum([float(row['Read Value'])
                    for row
                    in self.__get_data_since(since=since)])
    
    @property
    def today(self):
        return self.__sum_data_since(since=datetime.now().replace(hour=0, minute=0, second=0, microsecond=0))
    
    @property
    def last_24_hours(self):
        return self.__sum_data_since(since=datetime.now() - timedelta(days=1))
    
    @property
    def this_week(self):
        return self.__sum_data_since(since=datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=datetime.now().weekday()))
    
    @property
    def last_7_days(self):
        return self.__sum_data_since(since=datetime.now() - timedelta(days=7))
        
    @property
    def this_month(self):
        return self.__sum_data_since(since=datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    
    @property
    def last_30_days(self):
        return self.__sum_data_since(since=datetime.now() - timedelta(days=30))


class ESBCachingApi:
    """To not poll ESB constantly. The data only updates like once a day anyway."""
    
    def __init__(self, esb_api) -> None:
        self._esb_api = esb_api
        self._cached_data = None
        self._cached_data_timestamp = None
    
    async def fetch(self):
        if self._cached_data_timestamp is None or \
            self._cached_data_timestamp < datetime.now() - MIN_TIME_BETWEEN_UPDATES:
            try:
                self._cached_data = await self._esb_api.fetch()
                self._cached_data_timestamp = datetime.now()
            except Exception as err:
                LOGGER.error('Error fetching data: %s', err)
                self._cached_data = None
                self._cached_data_timestamp = None
                raise err

        return self._cached_data
    

class ESBDataApi:
    """Class for handling the data retrieval."""

    def __init__(self, *, hass, session, username, password, mprn):
        """Initialize the data object."""
        self._hass = hass
        self._session = session
        self._username = username
        self._password = password
        self._mprn = mprn

    def __login(self):
        session = requests.Session()

        # Get CSRF token and stuff
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0',
        }) 
        
        login_page = session.get('https://myaccount.esbnetworks.ie/', allow_redirects=True, timeout=10)
        
        settings_var = re.findall(r"(?<=var SETTINGS = )\S*;", str(login_page.content))[0][:-1]
        settings = json.loads(settings_var)

        request_1_response_cookies = session.cookies.get_dict()
        x_csrf_token = settings['csrf']
        transId = settings['transId']

        x_ms_cpim_sso = request_1_response_cookies.get('x-ms-cpim-sso:esbntwkscustportalprdb2c01.onmicrosoft.com_0')
        x_ms_cpim_csrf = request_1_response_cookies.get('x-ms-cpim-csrf')
        x_ms_cpim_trans = request_1_response_cookies.get('x-ms-cpim-trans')

        sleeping_delay= randint(10,20)
        sleep(sleeping_delay)

        # Login
       request_2_response = session.post('https://login.esbnetworks.ie/esbntwkscustportalprdb2c01.onmicrosoft.com/B2C_1A_signup_signin/SelfAsserted?tx=' + transId + '&p=B2C_1A_signup_signin', 
        data={
          'signInName': self._username, 
          'password': self._password, 
          'request_type': 'RESPONSE'
        },
        headers={
          'x-csrf-token': x_csrf_token,
          'User-Agent': 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0',
          'Accept': 'application/json, text/javascript, */*; q=0.01',
          'Accept-Language': 'en-US,en;q=0.5',
          'Accept-Encoding': 'gzip, deflate, br',
          'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
          'X-Requested-With': 'XMLHttpRequest',
          'Origin': 'https://login.esbnetworks.ie',
          'Dnt': '1',
          'Sec-Gpc': '1',
          'Sec-Fetch-Dest': 'empty',
          'Sec-Fetch-Mode': 'cors',
          'Sec-Fetch-Site': 'same-origin',
          'Priority': 'u=0',
          'Te': 'trailers',
        },
        cookies={
            'x-ms-cpim-csrf':request_1_response_cookies.get('x-ms-cpim-csrf'),
        #    'x-ms-cpim-sso:esbntwkscustportalprdb2c01.onmicrosoft.com_0':request_1_response_cookies.get('x-ms-cpim-sso:esbntwkscustportalprdb2c01.onmicrosoft.com_0'),
            'x-ms-cpim-trans':request_1_response_cookies.get('x-ms-cpim-trans'),
        },
        allow_redirects=False)

        request_2_response_cookies = session.cookies.get_dict()
        
        request_3_response = session.get(
            'https://login.esbnetworks.ie/esbntwkscustportalprdb2c01.onmicrosoft.com/B2C_1A_signup_signin/api/CombinedSigninAndSignup/confirmed',
            params={
              'rememberMe': False,
              'csrf_token': x_csrf_token,
              'tx': transId,
              'p': 'B2C_1A_signup_signin',
            },
            headers={
              "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
              "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
              "Accept-Language": "en-US,en;q=0.5",
              "Accept-Encoding": "gzip, deflate, br",
              "Dnt": "1",
              "Sec-Gpc": "1",
              "Sec-Fetch-Dest": "document",
              "Sec-Fetch-Mode": "navigate",
              "Sec-Fetch-Site": "same-origin",
              "Priority": "u=0, i",
              "Pragma": "no-cache",
              "Cache-Control": "no-cache",
              "Te": "trailers",
            },
            cookies={
                "x-ms-cpim-csrf":request_2_response_cookies.get("x-ms-cpim-csrf"),
            #    "x-ms-cpim-sso:esbntwkscustportalprdb2c01.onmicrosoft.com_0": request_2_response_cookies.get("x-ms-cpim-sso:esbntwkscustportalprdb2c01.onmicrosoft.com_0"),
                'x-ms-cpim-trans':request_2_response_cookies.get("x-ms-cpim-trans"),
            },
          )

        tester_soup = BeautifulSoup(request_3_response.content, 'html.parser')
        page_title = tester_soup.find("title")
        request_3_response_cookies = session.cookies.get_dict()
        
        return session
    
    def __fetch_data(self, requests_session):
        """Fetch the power usage data from ESB"""
        csv_data_response = requests_session.get('https://myaccount.esbnetworks.ie/DataHub/DownloadHdf?mprn=' + self._mprn,
                                                 timeout=10)
        csv_data_response.raise_for_status()
        csv_data = csv_data_response.content.decode('utf-8')

        return csv_data
    
    def __csv_to_dict(self, csv_data):
        reader = csv.DictReader(StringIO(csv_data))
        return [r for r in reader]

    async def fetch(self):
        session = await self._hass.async_add_executor_job(self.__login)
        csv_data = await self._hass.async_add_executor_job(self.__fetch_data, session)
        data = await self._hass.async_add_executor_job(self.__csv_to_dict, csv_data)
        
        return ESBData(data=data)

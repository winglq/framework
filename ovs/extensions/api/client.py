# Copyright 2015 CloudFounders NV
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Module for the OVS API client
"""

import urllib
import base64
import time
import requests


class OVSClient(object):
    """
    Represents the OVS client
    """

    def __init__(self, ip, port, credentials=None, verify=False):
        """
        Initializes the object with credentials and connection information
        """
        if credentials is not None and len(credentials) != 2:
            raise RuntimeError('Credentials should be None (no authentication) or a tuple containing client_id and client_secret (authenticated)')
        self.client_id = credentials[0] if credentials is not None else None
        self.client_secret = credentials[1] if credentials is not None else None
        self._url = 'https://{0}:{1}/api'.format(ip, port)
        self._token = None
        self._verify = verify

    def _connect(self):
        """
        Authenticates to the api
        """
        headers = {'Accept': 'application/json',
                   'Authorization': 'Basic {0}'.format(base64.b64encode('{0}:{1}'.format(self.client_id, self.client_secret)).strip())}
        response = requests.post(url='{0}/oauth2/token/'.format(self._url),
                                 data={'grant_type': 'client_credentials'},
                                 headers=headers,
                                 verify=self._verify).json()

        if len(response.keys()) == 1 and 'error' in response:
            raise RuntimeError(response['error'])
        self._token = response['access_token']

    def _prepare(self, **kwargs):
        """
        Prepares the call:
        * Authentication, if required
        * Preparing headers, returning them
        """
        if self.client_id is not None and self._token is None:
            self._connect()

        headers = {'Accept': 'application/json; version=*'}
        if self._token is not None:
            headers['Authorization'] = 'Bearer {0}'.format(self._token)

        params = ''
        if 'params' in kwargs and kwargs['params'] is not None:
            params = '?{0}'.format(urllib.urlencode(kwargs['params']))
        url = '{0}{{0}}{1}'.format(self._url, params)

        return headers, url

    @staticmethod
    def _process(response):
        """
        Processes a call result
        """
        if response.status_code == 403:
            raise RuntimeError('No access to the requested API')
        if response.status_code == 404:
            raise RuntimeError('The requested API could not be located')
        if response.status_code == 405:
            raise RuntimeError('Requested method not allowed')
        if response.status_code == 429:
            raise RuntimeError('The requested API has rate limiting: {0}'.format(response.text))
        if response.status_code == 500:
            raise RuntimeError('Received internal server error: {0}'.format(response.text))
        try:
            return_data = response.json()
            return return_data
        except:
            raise RuntimeError('Could not parse returned data: {0}: {1}'.format(response.status_code, response.text))

    def get(self, api, params=None):
        """
        Executes a GET call
        """
        headers, url = self._prepare(params=params)
        return OVSClient._process(requests.get(url=url.format(api),
                                               headers=headers,
                                               verify=self._verify))

    def post(self, api, data=None, params=None):
        """
        Executes a POST call
        """
        headers, url = self._prepare(params=params)
        return OVSClient._process(requests.post(url=url.format(api),
                                                data=data,
                                                headers=headers,
                                                verify=self._verify))

    def put(self, api, data=None, params=None):
        """
        Executes a PUT call
        """
        headers, url = self._prepare(params=params)
        return OVSClient._process(requests.put(url=url.format(api),
                                               data=data,
                                               headers=headers,
                                               verify=self._verify))

    def patch(self, api, data=None, params=None):
        """
        Executes a PATCH call
        """
        headers, url = self._prepare(params=params)
        return OVSClient._process(requests.patch(url=url.format(api),
                                                 data=data,
                                                 headers=headers,
                                                 verify=self._verify))

    def wait_for_task(self, task_id, timeout=None):
        """
        Waits for a task to complete
        """
        start = time.time()
        task_metadata = {'ready': False}
        while task_metadata['ready'] is False:
            if timeout is not None and timeout < (time.time() - start):
                raise RuntimeError('Waiting for task {0} has timed out.'.format(task_id))
            task_metadata = self.get('/tasks/{0}/'.format(task_id))
            if task_metadata['ready'] is False:
                time.sleep(1)
        return task_metadata['successful'], task_metadata['result']
import requests
import json
import sys


class rest_api_lib:
    def __init__(self, vmanage_ip, vmanage_port, username, password):
        self.vmanage_ip = vmanage_ip
        self.vmanage_port = vmanage_port
        self.session = {}
        self.login(username, password)

    def login(self, username, password):
        """Login to vmanage"""
        base_url_str = "https://" + str(self.vmanage_ip) + ":" + str(self.vmanage_port)

        login_action = '/j_security_check'

        # Format data for loginForm
        login_data = {'j_username': username, 'j_password': password}

        # Url for posting login data
        login_url = base_url_str + login_action

        sess = requests.session()
        # If the vmanage has a certificate signed by a trusted authority change verify to True
        login_response = sess.post(url=login_url, data=login_data, verify=False)

        if b'<html>' in login_response.content:
            print("Login Failed")
            sys.exit(0)

        self.session[self.vmanage_ip] = sess

    def get_request(self, mount_point):
        """GET request"""
        url = "https://" + str(self.vmanage_ip) + ":" + str(self.vmanage_port) + "/dataservice/" + mount_point

        response = self.session[self.vmanage_ip].get(url, verify=False)
        data = response.content
        return data

    def post_request(self, mount_point, payload, headers={'Content-Type': 'application/json'}):
        """POST request"""
        url = "https://" + str(self.vmanage_ip) + ":" + str(self.vmanage_port) + "/dataservice/" + mount_point
        payload = json.dumps(payload)
        print(payload)

        response = self.session[self.vmanage_ip].post(url=url, data=payload, headers=headers, verify=False)
        data = response.json()
        return data

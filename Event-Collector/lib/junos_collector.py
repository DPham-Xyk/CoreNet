import json
import logging
import logging.config
import time
import uuid
from copy import deepcopy
from datetime import datetime

import requests
import yaml
from jnpr.junos import Device
from jnpr.junos.exception import ConnectError

# Constants
DATABASE_URL = 'http://10.49.227.135'
DATABASE_PORT = 5000

# Logging
logger = logging.getLogger(__name__)


class JunosCollector(object):
    def __init__(self, config_path):
        """Collector module for Junos RPC information, statistics and status

        :param config_path: Location of the credentials for each network device
        :type config_path: str
        """
        self.connected_devices = {}
        self.network_devices = {}
        self.db_event_endpoint = '{}:{}/create_event'.format(DATABASE_URL, DATABASE_PORT)
        self._import_network_devices(config_path)
        self.start_monitoring()

    def start_monitoring(self):
        """Monitoring loop which collects information from each device
        for a specified interval
        """
        while True:
            # # Interface Status
            device_interface_statuses = self.get_interface_status()
            self.monitor_oper_status(device_interface_statuses)
            self.monitor_admin_status(device_interface_statuses)

            # BGP Peers
            bgp_down_count = self.get_bgp_peers()
            self.monitor_bgp_peers(bgp_down_count)

            # LDP Neighbors
            ldp_neighbors = self.get_ldp_session()
            self.monitor_ldp_session(ldp_neighbors)

            # OSPF Neighbors
            ospf_neighbors = self.get_ospf_neighbors()
            self.monitor_ospf_neighbors(ospf_neighbors)
            ospf_interfaces = self.get_ospf_interfaces()
            self.monitor_ospf_interfaces(ospf_interfaces)

            # PCEP Status
            pcep_statuses = self.get_pcep_statuses()
            self.monitor_pcep_statuses(pcep_statuses)
            time.sleep(30)

    def add_event_to_db(self, event_msg):
        """Sends collected information as events to the Database endpoint

        :param event_msg: Event message that is compatible with the Database schema
        :type event_msg: dict
        """
        headers = {
            'Content-Type': 'application/json',
        }
        requests.post(self.db_event_endpoint, json=event_msg, headers=headers)

    def _import_network_devices(self, network_device_file):
        """Import the hostnames, username and password for each network device
        and connect to the device

        :param network_device_file: Location of the credentials for each network device
        :type network_device_file: str
        """
        logger.debug('Loading network devices into JunosCollector')
        with open(network_device_file, 'r') as f:
            import_devices = yaml.load(f.read())

        for device in import_devices['devices']:
            self.network_devices[device['name']] = device
            logger.debug('Imported credentials for %s', device['name'])

        for _, device in self.network_devices.items():
            self._connect_to_device(device)

    def _connect_to_device(self, device):
        """Connects to the network device via Netconf

        :param device: Contains the necessary information to connect to the device
        :type device: dict
        :raises ConnectError: When a connection can not be establish to the device
        """
        try:
            logger.debug('Connecting to %s', device['ip'])
            dev = Device(host=device['ip'], user=device['user'], password=device['password']).open()
            logger.info('Successfully connected to %s', device['ip'])
        except ConnectError as e:
            logger.error('%s', str(e))
            raise ConnectError(e)
        else:
            self.connected_devices[device['name']] = dev

    def _create_event(self, name, type, priority, body):
        """Serialises an Event object that conforms with the Event Database schema

        :param name: Name of the Event
        :type name: str
        :param type  Type of Event (eg. CLI/APPFORMIX)
        :type type: str
        :param priority: Priority of the Event (eg. INFORMATION/WARNING/CRITICAL)
        :type priority: str
        :param body: Any other extra information related to the Event
        :type body: dict
        :return: An Event Database compatible object
        :rtype: dict
        """
        event = {
            'uuid': str(uuid.uuid4()),
            'time': str(datetime.now().isoformat()),
            'name': name,
            'type': type,
            'priority': priority,
            'body': json.dumps(body),
        }
        return event

    def get_interface_status(self):
        device_interface_statuses = {}
        rpc_replies = {}
        to_monitor = ['ge-0/0/0', 'ge-0/0/1', 'ge-0/0/2', 'ge-0/0/0.0', 'ge-0/0/1.0', 'ge-0/0/2.0']
        for dev_name, connected_dev in self.connected_devices.items():
            if connected_dev is None:
                return

            rpc_reply = connected_dev.rpc.get_interface_information(terse=True)
            rpc_replies[dev_name] = rpc_reply

        for dev_name, rpc_reply in rpc_replies.items():
            device_interface_statuses[dev_name] = []
            interface_status = {}
            logical_interfaces = rpc_reply.xpath('//physical-interface|//logical-interface')

            for logical_interface in logical_interfaces:
                name = logical_interface.xpath('.//name')[0].text.replace('\n', '')
                if name not in to_monitor:
                    continue
                admin_status = logical_interface.xpath('.//admin-status')[0].text.replace('\n', '')
                oper_status = logical_interface.xpath('.//oper-status')[0].text.replace('\n', '')
                interface_status[name] = {
                    'admin-status': admin_status,
                    'oper-status': oper_status,
                }
            device_interface_statuses[dev_name] = interface_status

        # self.interface_status = device_interface_statuses
        return device_interface_statuses

    def get_bgp_peers(self):
        device_bgp_peer_count = {}
        rpc_replies = {}
        to_monitor = ['P1', 'P2', 'P3']
        for dev_name, connected_dev in self.connected_devices.items():
            if connected_dev is None:
                return
            if dev_name not in to_monitor:
                continue

            rpc_reply = connected_dev.rpc.get_bgp_summary_information()
            rpc_replies[dev_name] = rpc_reply

        for dev_name, rpc_reply in rpc_replies.items():
            device_bgp_peer_count[dev_name] = {}
            device_bgp_peer_count[dev_name]['peer-count'] = 0
            device_bgp_peer_count[dev_name]['down-peer-count'] = 0

            try:
                peer_count_xpath = rpc_reply.xpath('//bgp-information/peer-count')
                down_peer_count_xpath = rpc_reply.xpath('//bgp-information/down-peer-count')

                if peer_count_xpath:
                    device_bgp_peer_count[dev_name]['peer-count'] = int(peer_count_xpath[0].text)
                if peer_count_xpath:
                    device_bgp_peer_count[dev_name]['down-peer-count'] = int(down_peer_count_xpath[0].text)
            except Exception as e:
                logger.error(e)

        return device_bgp_peer_count

    def get_ldp_session(self):
        ldp_neighbors = {}
        rpc_replies = {}
        to_monitor = ['P1', 'P2', 'P3', 'PE1', 'PE2', 'PE3', 'PE4']
        for dev_name, connected_dev in self.connected_devices.items():
            if connected_dev is None:
                return
            if dev_name not in to_monitor:
                continue

            rpc_reply = connected_dev.rpc.get_ldp_session_information()
            rpc_replies[dev_name] = rpc_reply

        for dev_name, rpc_reply in rpc_replies.items():
            ldp_neighbors[dev_name] = {}
            ldp_neighbors[dev_name]['ldp-session-state'] = ''
            ldp_neighbors[dev_name]['ldp-neighbor-address'] = ''
            try:
                ldp_session_xpath = rpc_reply.xpath('//ldp-session-information/ldp-session')

                for ldp_session in ldp_session_xpath:
                    ldp_neighbors[dev_name]['ldp-session-state'] = ldp_session.xpath('.//ldp-session-state')[0].text
                    ldp_neighbors[dev_name]['ldp-neighbor-address'] = ldp_session.xpath('.//ldp-neighbor-address')[0].text
            except Exception as e:
                logger.error(e)

        return ldp_neighbors

    def get_ospf_neighbors(self):
        o_ospf_neighbors = {}
        rpc_replies = {}
        to_monitor = ['P1', 'P2', 'P3', 'PE1', 'PE2', 'PE3', 'PE4']
        for dev_name, connected_dev in self.connected_devices.items():
            if connected_dev is None:
                return
            if dev_name not in to_monitor:
                continue

            rpc_reply = connected_dev.rpc.get_ospf_neighbor_information()
            rpc_replies[dev_name] = rpc_reply

        for dev_name, rpc_reply in rpc_replies.items():
            o_ospf_neighbors[dev_name] = {}
            o_ospf_neighbors[dev_name]['neighbor-address'] = ''
            o_ospf_neighbors[dev_name]['ospf-neighbor-state'] = ''
            o_ospf_neighbors[dev_name]['neighbor-id'] = ''
            o_ospf_neighbors[dev_name]['interface-name'] = ''
            try:
                ospf_neighbor_xpath = rpc_reply.xpath('//ospf-neighbor-information/ospf-neighbor')
                for ospf_neighbor in ospf_neighbor_xpath:
                    o_ospf_neighbors[dev_name]['neighbor-address'] = ospf_neighbor.xpath('.//neighbor-address')[0].text
                    o_ospf_neighbors[dev_name]['ospf-neighbor-state'] = ospf_neighbor.xpath('.//ospf-neighbor-state')[0].text
                    o_ospf_neighbors[dev_name]['neighbor-id'] = ospf_neighbor.xpath('.//neighbor-id')[0].text
                    o_ospf_neighbors[dev_name]['interface-name'] = ospf_neighbor.xpath('.//interface-name')[0].text
            except Exception as e:
                logger.error(e)
        return o_ospf_neighbors

    def get_ospf_interfaces(self):
        o_ospf_interfaces = {}
        rpc_replies = {}
        to_monitor = ['P1', 'P2', 'P3', 'PE1', 'PE2', 'PE3', 'PE4']

        ospf_interfaces_template = {}
        ospf_interfaces_template['interface-name'] = ''
        ospf_interfaces_template['ospf-area'] = ''
        ospf_interfaces_template['ospf-interface-state'] = ''

        for dev_name, connected_dev in self.connected_devices.items():
            if connected_dev is None:
                return
            if dev_name not in to_monitor:
                continue

            rpc_reply = connected_dev.rpc.get_ospf_interface_information()
            rpc_replies[dev_name] = rpc_reply

        for dev_name, rpc_reply in rpc_replies.items():
            try:
                ospf_interfaces_xpath = rpc_reply.xpath('//ospf-interface-information/ospf-interface')
                o_ospf_interfaces[dev_name] = []
                for i, ospf_interface in enumerate(ospf_interfaces_xpath):
                    o_ospf_interface = deepcopy(ospf_interfaces_template)
                    o_ospf_interface['interface-name'] = ospf_interface.xpath('.//interface-name')[0].text
                    o_ospf_interface['ospf-area'] = ospf_interface.xpath('.//ospf-area')[0].text
                    o_ospf_interface['ospf-interface-state'] = ospf_interface.xpath('.//ospf-interface-state')[0].text
                    o_ospf_interfaces[dev_name].append(o_ospf_interface)
            except Exception as e:
                logger.error(e)

        return o_ospf_interfaces

    def get_pcep_statuses(self):
        o_pcep_statuses = {}
        rpc_replies = {}
        to_monitor = ['P1', 'P2', 'P3', 'PE1', 'PE2', 'PE3', 'PE4']

        pcep_statuses_template = {}
        pcep_statuses_template['session-name'] = ''
        pcep_statuses_template['session-type'] = ''
        pcep_statuses_template['session-provisioning'] = ''
        pcep_statuses_template['session-status'] = ''

        for dev_name, connected_dev in self.connected_devices.items():
            if connected_dev is None:
                return
            if dev_name not in to_monitor:
                continue

            rpc_reply = connected_dev.rpc.get_path_computation_client_status()
            rpc_replies[dev_name] = rpc_reply

        for dev_name, rpc_reply in rpc_replies.items():
            try:
                pcep_status_xpath = rpc_reply.xpath('//path-computation-client-status/pcc-status-sessions/pcc-status-sessions-entry')
                o_pcep_statuses[dev_name] = []
                for i, pcep_status in enumerate(pcep_status_xpath):
                    o_pcep_status = deepcopy(pcep_statuses_template)
                    o_pcep_status['session-name'] = pcep_status.xpath('.//session-name')[0].text
                    o_pcep_status['session-type'] = pcep_status.xpath('.//session-type')[0].text
                    o_pcep_status['session-provisioning'] = pcep_status.xpath('.//session-provisioning')[0].text
                    o_pcep_status['session-status'] = pcep_status.xpath('.//session-status')[0].text
                    o_pcep_statuses[dev_name].append(o_pcep_status)
            except Exception as e:
                logger.error(e)
        return o_pcep_statuses

    def monitor_oper_status(self, interface_status):
        for device_name, interfaces in interface_status.items():
            oper_status = []
            for interface_name, interface in interfaces.items():
                if interface['oper-status'] == 'down':
                    oper_status.append(interface_name)

            if oper_status:
                event = self._create_event(name='oper_status.interface.down.{}'.format(device_name),
                                           type='cli',
                                           priority='critical',
                                           body={device_name: oper_status})
                self.add_event_to_db(event)
                logger.info('%s - %s - %s', event['uuid'], event['time'], event['name'])
            else:
                event = self._create_event(name='oper_status.interface.up.{}'.format(device_name),
                                           type='cli',
                                           priority='information',
                                           body={device_name: oper_status})
                self.add_event_to_db(event)
                logger.info('%s - %s - %s', event['uuid'], event['time'], event['name'])

    def monitor_admin_status(self, interface_status):
        for device_name, interfaces in interface_status.items():
            admin_status = []
            for interface_name, interface in interfaces.items():
                if interface['admin-status'] == 'down':
                    admin_status.append(interface_name)

            if admin_status:
                event = self._create_event(name='admin_status.interface.down.{}'.format(device_name),
                                           type='cli',
                                           priority='critical',
                                           body={device_name: admin_status})
                self.add_event_to_db(event)
                logger.info('%s - %s - %s', event['uuid'], event['time'], event['name'])
            else:
                event = self._create_event(name='admin_status.interface.up.{}'.format(device_name),
                                           type='cli',
                                           priority='information',
                                           body={device_name: admin_status})
                self.add_event_to_db(event)
                logger.info('%s - %s - %s', event['uuid'], event['time'], event['name'])

    def monitor_bgp_peers(self, bgp_peer_count):
        for device_name, bgp_peer_count in bgp_peer_count.items():

            if bgp_peer_count['down-peer-count'] == 0:
                event = self._create_event(name='bgp.peers.up.{}'.format(device_name),
                                           type='cli',
                                           priority='information',
                                           body={device_name: {
                                                'up-peer-count': bgp_peer_count['peer-count'] - bgp_peer_count['down-peer-count'],
                                                'down-peer-count': bgp_peer_count['down-peer-count'],
                                           }})
                self.add_event_to_db(event)
                logger.info('%s - %s - %s', event['uuid'], event['time'], event['name'])
            else:
                event = self._create_event(name='bgp.peers.down.{}'.format(device_name),
                                           type='cli',
                                           priority='critical',
                                           body={device_name: {
                                                'up-peer-count': bgp_peer_count['peer-count'] - bgp_peer_count['down-peer-count'],
                                                'down-peer-count': bgp_peer_count['down-peer-count'],
                                           }})
                self.add_event_to_db(event)
                logger.info('%s - %s - %s', event['uuid'], event['time'], event['name'])

    def monitor_ldp_session(self, ldp_neighbors):
        for device_name, ldp_neighbor in ldp_neighbors.items():
            if ldp_neighbor['ldp-session-state'] == 'Operational':
                event = self._create_event(name='ldp.session.up.{}'.format(device_name),
                                           type='cli',
                                           priority='information',
                                           body={device_name: ldp_neighbor})
                self.add_event_to_db(event)
                logger.info('%s - %s - %s', event['uuid'], event['time'], event['name'])
            else:
                event = self._create_event(name='ldp.session.down.{}'.format(device_name),
                                           type='cli',
                                           priority='critical',
                                           body={device_name: ldp_neighbor})
                self.add_event_to_db(event)
                logger.info('%s - %s - %s', event['uuid'], event['time'], event['name'])

    def monitor_ospf_neighbors(self, ospf_neighbors):
        for device_name, ospf_neighbor in ospf_neighbors.items():
            if ospf_neighbor['ospf-neighbor-state'] == 'Full':
                event = self._create_event(name='ospf.neighbors.up.{}'.format(device_name),
                                           type='cli',
                                           priority='information',
                                           body={device_name: ospf_neighbor})
                self.add_event_to_db(event)
                logger.info('%s - %s - %s', event['uuid'], event['time'], event['name'])
            else:
                event = self._create_event(name='ospf.neighbors.down.{}'.format(device_name),
                                           type='cli',
                                           priority='critical',
                                           body={device_name: ospf_neighbor})
                self.add_event_to_db(event)
                logger.info('%s - %s - %s', event['uuid'], event['time'], event['name'])

    def monitor_pcep_statuses(self, pcep_statuses):
        for device_name, pcep_status in pcep_statuses.items():
            status = True
            for pcep_session in pcep_status:
                if pcep_session['session-status'] != 'Up':
                    status = False
                    break

            if status is True:
                event = self._create_event(name='pcep.status.up.{}'.format(device_name),
                                           type='cli',
                                           priority='information',
                                           body={device_name: pcep_status})
                self.add_event_to_db(event)
                logger.info('%s - %s - %s', event['uuid'], event['time'], event['name'])
            else:
                event = self._create_event(name='pcep.status.down.{}'.format(device_name),
                                           type='cli',
                                           priority='critical',
                                           body={device_name: pcep_status})
                self.add_event_to_db(event)
                logger.info('%s - %s - %s', event['uuid'], event['time'], event['name'])

    def monitor_ospf_interfaces(self, d_ospf_interfaces):
        for device_name, ospf_interfaces in d_ospf_interfaces.items():
            status = True
            for ospf_interface in ospf_interfaces:
                if ospf_interface['ospf-interface-state'] == 'Down':
                    status = False
                    break

            if status is True:
                event = self._create_event(name='ospf.interfaces.up.{}'.format(device_name),
                                           type='cli',
                                           priority='information',
                                           body={device_name: ospf_interfaces})
                self.add_event_to_db(event)
                logger.info('%s - %s - %s', event['uuid'], event['time'], event['name'])
            else:
                event = self._create_event(name='ospf.interfaces.down.{}'.format(device_name),
                                           type='cli',
                                           priority='critical',
                                           body={device_name: ospf_interfaces})
                self.add_event_to_db(event)
                logger.info('%s - %s - %s', event['uuid'], event['time'], event['name'])


if __name__ == '__main__':
    jc = JunosCollector(device_config='../config/devices.yaml')

"""
Provides an asynchronous, crypto and compression aware socket for connecting to
servers and processing incoming packet data.
Coordinates with the Timers plugin to honor clock-time timers
"""

import sys
import socket
import select
import time
from spock import utils
from spock.utils import pl_announce
from spock.mcp import mcpacket, mcdata, mccrypto

class SelectSocket:
	def __init__(self, timer):
		self.sending = False
		self.timer = timer
		self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self.sock.setblocking(False)
		self.recv = self.sock.recv
		self.send = self.sock.send

	def poll(self):
		flags = []
		if self.sending:
			self.sending = False
			slist = [(self.sock,), (self.sock,), (self.sock,)]
		else:
			slist = [(self.sock,), (), (self.sock,)]
		timeout = self.timer.get_timeout()
		if timeout>=0:
			slist.append(timeout)
		try:
			rlist, wlist, xlist = select.select(*slist)
		except select.error as e:
			print(str(e))
			rlist = []
			wlist = []
			xlist = []
		if rlist:         flags.append('SOCKET_RECV')
		if wlist:         flags.append('SOCKET_SEND')
		if xlist:         flags.append('SOCKET_ERR')
		return flags

	def reset(self):
		self.sock.close()
		self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self.sock.setblocking(False)

class NetCore:
	def __init__(self, sock, event):
		self.sock = sock
		self.event = event
		self.host = None
		self.port = None
		self.connected = False
		self.encrypted = False
		self.proto_state = mcdata.HANDSHAKE_STATE
		self.comp_state = mcdata.PROTO_COMP_OFF
		self.comp_threshold = -1
		self.sbuff = b''
		self.rbuff = utils.BoundBuffer()

	def connect(self, host = 'localhost', port = 25565):
		self.host = host
		self.port = port
		try:
			print("Attempting to connect to host:", host, "port:", port)
			#Set the connect to be a blocking operation
			self.sock.sock.setblocking(True)
			self.sock.sock.connect((self.host, self.port))
			self.sock.sock.setblocking(False)
			self.connected = True
			self.event.emit('connect', (self.host, self.port))
			print("Connected to host:", host, "port:", port)
		except socket.error as error:
			print("Error on Connect:", str(error))

	def set_proto_state(self, state):
		self.proto_state = state
		self.event.emit(mcdata.state_lookup[state] + '_STATE')

	def set_comp_state(self, threshold):
		self.comp_threshold = threshold
		if threshold >=0:
			self.comp_state = mcdata.PROTO_COMP_ON

	def push(self, packet):
		data = packet.encode(self.comp_state, self.comp_threshold)
		self.sbuff += (self.cipher.encrypt(data) if self.encrypted else data)
		self.event.emit(packet.ident, packet)
		self.event.emit(packet.str_ident, packet)
		self.sock.sending = True

	def push_packet(self, ident, data):
		self.push(mcpacket.Packet(ident, data))

	def read_packet(self, data = b''):
		self.rbuff.append(self.cipher.decrypt(data) if self.encrypted else data)
		while True:
			self.rbuff.save()
			try:
				packet = mcpacket.Packet(ident = (
					self.proto_state,
					mcdata.SERVER_TO_CLIENT,
				)).decode(self.rbuff, self.comp_state)
			except utils.BufferUnderflowException:
				self.rbuff.revert()
				break
			if packet:
				self.event.emit(packet.ident, packet)
				self.event.emit(packet.str_ident, packet)

	def enable_crypto(self, secret_key):
		self.cipher = mccrypto.AESCipher(secret_key)
		self.encrypted = True

	def disable_crypto(self):
		self.cipher = None
		self.encrypted = False

	def reset(self):
		self.connected = False
		self.sock.reset()
		self.__init__(self.sock, self.event)

	disconnect = reset

@pl_announce('Net')
class NetPlugin:
	def __init__(self, ploader, settings):
		settings = ploader.requires('Settings')
		self.bufsize = settings['bufsize']
		self.sock_quit = settings['sock_quit']
		self.event = ploader.requires('Event')
		self.timer = ploader.requires('Timers')
		self.sock = SelectSocket(self.timer)
		self.net = NetCore(self.sock, self.event)
		ploader.provides('Net', self.net)

		ploader.reg_event_handler('event_tick', self.tick)
		ploader.reg_event_handler('SOCKET_RECV', self.handleRECV)
		ploader.reg_event_handler('SOCKET_SEND', self.handleSEND)
		ploader.reg_event_handler('SOCKET_ERR', self.handleERR)
		ploader.reg_event_handler('SOCKET_HUP', self.handleHUP)
		ploader.reg_event_handler('PLAY<Disconnect', self.handle_disconnect)
		ploader.reg_event_handler('HANDSHAKE>Handshake', self.handle_handshake)
		ploader.reg_event_handler('LOGIN<Login Success', self.handle_login_success)
		ploader.reg_event_handler('LOGIN<Set Compression', self.handle_comp)
		ploader.reg_event_handler('PLAY<Set Compression', self.handle_comp)

	def tick(self, name, data):
		if self.net.connected:
			for flag in self.sock.poll():
				self.event.emit(flag)
		else:
			timeout = self.timer.get_timeout()
			if timeout == -1:
				time.sleep(1)
			else:
				time.sleep(timeout)


	#SOCKET_RECV - Socket is ready to recieve data
	def handleRECV(self, name, data):
		if self.net.connected:
			try:
				data = self.sock.recv(self.bufsize)
				#print('read:', len(data))
				if not data: #Just because we have to support socket.select
					self.event.emit('SOCKET_HUP')
					return
				self.net.read_packet(data)
			except socket.error as error:
				self.event.emit('SOCKET_ERR', error)


	#SOCKET_SEND - Socket is ready to send data and Send buffer contains data to send
	def handleSEND(self, name, data):
		if self.net.connected:
			try:
				sent = self.sock.send(self.net.sbuff)
				self.net.sbuff = self.net.sbuff[sent:]
				if self.net.sbuff:
					self.sending = True
			except socket.error as error:
				print(error)
				self.event.emit('SOCKET_ERR', error)

	#SOCKET_ERR - Socket Error has occured
	def handleERR(self, name, data):
		self.net.reset()
		print("Socket Error:", data)
		self.event.emit('disconnect', data)
		if self.sock_quit and not self.event.kill_event:
			self.event.kill()

	#SOCKET_HUP - Socket has hung up
	def handleHUP(self, name, data):
		self.net.reset()
		print("Socket has hung up")
		self.event.emit('disconnect', "Socket Hung Up")
		if self.sock_quit and not self.event.kill_event:
			self.event.kill()

	#Handshake - Change to whatever the next state is going to be
	def handle_handshake(self, name, packet):
		self.net.set_proto_state(packet.data['next_state'])

	#Login Success - Change to Play state
	def handle_login_success(self, name, packet):
		self.net.set_proto_state(mcdata.PLAY_STATE)

	#Handle Set Compression packets
	def handle_comp(self, name, packet):
		self.net.set_comp_state(packet.data['threshold'])

	def handle_disconnect(self, name, packet):
		print("Disconnected:", packet.data['reason'])
		self.event.emit('disconnect', packet.data['reason'])

import spock.net.client
from spock.net.timer import Timer
from packet_queue import PacketQueue

class RikerClient(spock.net.client.Client):
	def __init__(self):
		super(RikerClient, self).__init__()
		self.move_queue = PacketQueue()
		self.move_timer = Timer(.05, self._send_move)

	def push_move(self, packet):
		self.move_queue.push(packet)

	def _send_move(self):
		if self.move_queue:
			self.push(self.move_queue.pop())
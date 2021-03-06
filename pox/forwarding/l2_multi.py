# Copyright 2012-2013 James McCauley
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
A shortest-path forwarding application.

This is a standalone L2 switch that learns ethernet addresses
across the entire network and picks short paths between them.

You shouldn't really write an application this way -- you should
keep more state in the controller (that is, your flow tables),
and/or you should make your topology more static.  However, this
does (mostly) work. :)

Depends on openflow.discovery
Works with openflow.spanning_tree
"""

from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.revent import *
from pox.lib.recoco import Timer
from collections import defaultdict
from pox.openflow.discovery import Discovery
from pox.lib.util import dpid_to_str
import time
from pox.openflow.spanning_tree import generator_for_link
import networkx as nx
import itertools

log = core.getLogger()

# Adjacency map.  [sw1][sw2] -> port from sw1 to sw2
adjacency = defaultdict(lambda:defaultdict(lambda:None))

# Switches we know of.  [dpid] -> Switch
switches = {}

clouds = {}

# ethaddr -> (switch, port)
mac_map = {}

# [sw1][sw2] -> (distance, intermediate)
path_map = defaultdict(lambda:defaultdict(lambda: {}))

real__path_map = defaultdict(lambda:defaultdict(lambda:set()))

# Waiting path.  (dpid,xid)->WaitingPath
waiting_paths = {}

# Time to not flood in seconds
FLOOD_HOLDDOWN = 5

# Flow timeouts
FLOW_IDLE_TIMEOUT = 10
FLOW_HARD_TIMEOUT = 30

# How long is allowable to set up a path?
PATH_SETUP_TIME = 4

broadcast_adj = set()

def _calc_paths ():
  """
  Essentially Floyd-Warshall algorithm
  """

  def dump ():
    for i in sws:
      for j in sws:
        a = path_map[i][j][0]
        #a = adjacency[i][j]
        if a is None: a = "*"
        print a,
      print

  def __get_path(source, dest):
    if source is dest: return None

    intermediate = path_map[source][dest]['intermediate']
    distance = path_map[source][dest]['distance']
    if distance == 1:
      return frozenset([tuple()])

    # print('intermediate %s' % str(path_map[source][dest]))

    def sub_path(k):
      if k == source or k == dest:
        return frozenset()
      else:
        left = map(lambda route: route + (k,), __get_path(source, k))
        right = __get_path(k, dest)
        # print('left = %s' % left)
        # print('right = %s' % right)
        return reduce(lambda result_set, product: result_set.union([product[0] + product[1]]),
                      itertools.product(left, right), frozenset())

    result = reduce(frozenset.union, map(lambda k: sub_path(k), intermediate))
    return result

  sws = switches.values()
  path_map.clear()
  for k in sws:
    for j in sws:
      path_map[k][j]['distance'] = float("inf")
      path_map[k][j]['intermediate'] = []
      path_map[k][j]['path'] = {}
    for j,port in adjacency[k].iteritems():
      if port is None: continue
      path_map[k][j]['distance'] = 1
      path_map[k][j]['intermediate'] = []
    path_map[k][k]['distance'] = 0
    path_map[k][k]['intermediate'] = []


  # dump()


  for k in sws:
    for i in sws:
      for j in sws:
            # i -> k -> j exists
            ikj_dist = path_map[i][k]['distance']+path_map[k][j]['distance']
            if ikj_dist < path_map[i][j]['distance']:
              # i -> k -> j is better than existing
              path_map[i][j]['distance'] = ikj_dist
              path_map[i][j]['intermediate'] = [k]


            elif path_map[i][j]['distance'] == ikj_dist:
             path_map[i][j]['intermediate'].append(k)

  for i in sws:
    for j in sws:
      all_intermediates = __get_path(i,j)
      if all_intermediates != None:
        for every_intermediate in all_intermediates:
          path = tuple([i] + list(every_intermediate) + [j])

          path_port = []

          for s1, s2 in zip(path[:-1],path[1:]):
            out_port = adjacency[s1][s2]
            path_port.append((s1,out_port))

          this_path_weight = 0
          for sw,port in path_port:
            this_port_congesion_status = sw.port_congestion[port]
            this_path_weight += this_port_congesion_status

          # this_path_weight = reduce(lambda x,y : x + y[0].port_congestion[y[1]], path_port)

          sw, port = max(path_port,key = lambda x: x[0].port_congestion[x[1]])
          this_path_max_congestion = sw.port_congestion[port]
          path_congestion_weight = [path,path_port,this_path_max_congestion,this_path_weight]

          for sw, port in path_port:
            sw.port_to_path[port].append(path_congestion_weight)

          path_map[i][j]['path'][path] = path_congestion_weight

        select_best_path_build_hash_dict(i,j)
  #print "--------------------"
  #dump()
def select_best_path_build_hash_dict(src,dst):
  min_congestion_path = min(path_map[src][dst]['path'].itervalues(), key=lambda x: x[2])
  min_congestion_on_path = min_congestion_path[2]
  min_congestion_paths = filter(lambda x: x[2] == min_congestion_on_path,path_map[src][dst]['path'].values())

  min_weight_path = min(min_congestion_paths, key=lambda x: x[-1])
  min_weight_on_path = min_weight_path[-1]
  min_weight_paths = filter(lambda x: x[-1] == min_weight_on_path,min_congestion_paths)

  path_map[src][dst]['best_con_weight'] = (min_congestion_on_path,min_weight_on_path)
  path_map[src][dst]['hash_dict'] = {}
  possible_path_index = 0
  for index in xrange(256):
    path_map[src][dst]['hash_dict'][index] = min_weight_paths[possible_path_index]
    if min_weight_paths[possible_path_index] is min_weight_paths[-1]:
      possible_path_index = 0
    else:
      possible_path_index += 1

def _check_path (p):
  """
  Make sure that a path is actually a string of nodes with connected ports

  returns True if path is valid
  """
  for a,b in zip(p[:-1],p[1:]):
    if adjacency[a[0]][b[0]] != a[2]:
      return False
    if adjacency[b[0]][a[0]] != b[1]:
      return False
  return True

def _path_selector(src,dst,match):
  source_mac = ord(match.dl_src._value[5])
  dest_mac = ord(match.dl_dst._value[5])
  hash_result_index = source_mac^dest_mac
  return path_map[src][dst]['hash_dict'][hash_result_index][0]

def _get_path (src, dst, first_port, final_port, match):
  """
  Gets a cooked path -- a list of (node,in_port,out_port)
  """
  # Start with a raw path...
  if len(path_map) == 0: _calc_paths()

  if src == dst:
    path = [src]
  else:
    path = _path_selector(src,dst,match)
    if path is None: return None

  # Now add the ports
  r = []
  in_port = first_port
  for s1,s2 in zip(path[:-1],path[1:]):
    out_port = adjacency[s1][s2]
    r.append((s1,in_port,out_port))
    in_port = adjacency[s2][s1]
  r.append((dst,in_port,final_port))

  assert _check_path(r), "Illegal path!"

  return r

def _is_edge_port_in_topo(id,port):
  if port in adjacency[id].values():
    return False
  else:
    return True


class WaitingPath (object):
  """
  A path which is waiting for its path to be established
  """
  def __init__ (self, path, packet):
    """
    xids is a sequence of (dpid,xid)
    first_switch is the DPID where the packet came from
    packet is something that can be sent in a packet_out
    """
    self.expires_at = time.time() + PATH_SETUP_TIME
    self.path = path
    self.first_switch = path[0][0].dpid
    self.xids = set()
    self.packet = packet

    if len(waiting_paths) > 1000:
      WaitingPath.expire_waiting_paths()

  def add_xid (self, dpid, xid):
    self.xids.add((dpid,xid))
    waiting_paths[(dpid,xid)] = self

  @property
  def is_expired (self):
    return time.time() >= self.expires_at

  def notify (self, event):
    """
    Called when a barrier has been received
    """
    self.xids.discard((event.dpid,event.xid))
    if len(self.xids) == 0:
      # Done!
      if self.packet:
        log.debug("Sending delayed packet out %s"
                  % (dpid_to_str(self.first_switch),))
        msg = of.ofp_packet_out(data=self.packet,
            action=of.ofp_action_output(port=of.OFPP_TABLE))
        core.openflow.sendToDPID(self.first_switch, msg)

      core.l2_multi.raiseEvent(PathInstalled(self.path))


  @staticmethod
  def expire_waiting_paths ():
    packets = set(waiting_paths.values())
    killed = 0
    for p in packets:
      if p.is_expired:
        killed += 1
        for entry in p.xids:
          waiting_paths.pop(entry, None)
    if killed:
      log.error("%i paths failed to install" % (killed,))


class PathInstalled (Event):
  """
  Fired when a path is installed
  """
  def __init__ (self, path):
    self.path = path


class Switch (EventMixin):
  def __init__ (self):
    self.connection = None
    self.ports = None
    self.dpid = None
    self._listeners = None
    self._connected_at = None
    self.port_congestion = {}
    self.port_to_path = {}

  def __init_port_congestion_and_port_to_path(self):
    for port in self.ports:
      if port.port_no >of.OFPP_MAX: continue
      self.port_congestion[port.port_no] = 0
      self.port_to_path[port.port_no] = []


  def update_congestion_path(self,port):
    new_congestion = self.port_congestion[port]
    for path in self.port_to_path[port]:

      src = path[0][0]
      dest = path[0][-1]

      new_path_weight = 0
      for sw,port in path[1]:
        this_port_congesion_status = sw.port_congestion[port]
        new_path_weight += this_port_congesion_status
      path[3] = new_path_weight
      if new_congestion > path[2]:
        path[2] = new_congestion
      select_best_path_build_hash_dict(src,dest)





  def __repr__ (self):
    return str(self.dpid)
    # return dpid_to_str(self.dpid)

  def _install (self, switch, in_port, out_port, match, buf = None):
    msg = of.ofp_flow_mod()
    msg.match = match
    msg.match.in_port = in_port
    msg.idle_timeout = FLOW_IDLE_TIMEOUT
    msg.hard_timeout = FLOW_HARD_TIMEOUT
    msg.actions.append(of.ofp_action_output(port = out_port))
    msg.buffer_id = buf
    if switch.connection is None: return
    switch.connection.send(msg)

  def _install_path (self, p, match, packet_in=None):
    wp = WaitingPath(p, packet_in)
    for sw,in_port,out_port in p:
      self._install(sw, in_port, out_port, match)
      msg = of.ofp_barrier_request()
      if sw.connection is None: pass
      sw.connection.send(msg)
      wp.add_xid(sw.dpid,msg.xid)

  def install_path (self, dst_sw, last_port, match, event):
    """
    Attempts to install a path between this switch and some destination
    """
    p = _get_path(self, dst_sw, event.port, last_port,match)
    if p is None:
      log.warning("Can't get from %s to %s", match.dl_src, match.dl_dst)

      import pox.lib.packet as pkt

      if (match.dl_type == pkt.ethernet.IP_TYPE and
          event.parsed.find('ipv4')):
        # It's IP -- let's send a destination unreachable
        log.debug("Dest unreachable (%s -> %s)",
                  match.dl_src, match.dl_dst)

        from pox.lib.addresses import EthAddr
        e = pkt.ethernet()
        e.src = EthAddr(dpid_to_str(self.dpid)) #FIXME: Hmm...
        e.dst = match.dl_src
        e.type = e.IP_TYPE
        ipp = pkt.ipv4()
        ipp.protocol = ipp.ICMP_PROTOCOL
        ipp.srcip = match.nw_dst #FIXME: Ridiculous
        ipp.dstip = match.nw_src
        icmp = pkt.icmp()
        icmp.type = pkt.ICMP.TYPE_DEST_UNREACH
        icmp.code = pkt.ICMP.CODE_UNREACH_HOST
        orig_ip = event.parsed.find('ipv4')

        d = orig_ip.pack()
        d = d[:orig_ip.hl * 4 + 8]
        import struct
        d = struct.pack("!HH", 0,0) + d #FIXME: MTU
        icmp.payload = d
        ipp.payload = icmp
        e.payload = ipp
        msg = of.ofp_packet_out()
        msg.actions.append(of.ofp_action_output(port = event.port))
        msg.data = e.pack()
        self.connection.send(msg)

      return

    log.debug("Installing path for %s -> %s %04x (%i hops)",
        match.dl_src, match.dl_dst, match.dl_type, len(p))

    # We have a path -- install it
    p = filter(lambda x:type(x[0]) is Switch,p)
    self._install_path(p, match, event.ofp)

    # Now reverse it and install it backwards
    # (we'll just assume that will work)
    p = [(sw,out_port,in_port) for sw,in_port,out_port in p]
    self._install_path(p, match.flip())


  def _handle_PacketIn (self, event):
    def flood ():
      """ Floods the packet """
      if self.is_holding_down:
        log.warning("Not flooding -- holddown active")
      msg = of.ofp_packet_out()
      # OFPP_FLOOD is optional; some switches may need OFPP_ALL
      msg.actions.append(of.ofp_action_output(port = of.OFPP_FLOOD))
      msg.buffer_id = event.ofp.buffer_id
      msg.in_port = event.port
      self.connection.send(msg)

    def drop ():
      # Kill the buffer
      if event.ofp.buffer_id is not None:
        msg = of.ofp_packet_out()
        msg.buffer_id = event.ofp.buffer_id
        event.ofp.buffer_id = None # Mark is dead
        msg.in_port = event.port
        self.connection.send(msg)

    packet = event.parsed

    loc = (self, event.port) # Place we saw this ethaddr
    dpid_port = (loc[0].dpid, loc[1])

    for cloud in clouds.values():
        if dpid_port in cloud.ports:
          loc = (cloud, 0)

    oldloc = mac_map.get(packet.src) # Place we last saw this ethaddr

    if packet.effective_ethertype == packet.LLDP_TYPE:
      drop()
      return

    if oldloc is None:
      if packet.src.is_multicast == False:
        mac_map[packet.src] = loc # Learn position for ethaddr
        log.debug("Learned %s at %s.%i", packet.src, loc[0], loc[1])
    elif oldloc != loc:
      # ethaddr seen at different place!
      if _is_edge_port_in_topo(loc[0],loc[1]):
        # New place is another "plain" port (probably)
        log.debug("%s moved from %s.%i to %s.%i?", packet.src,
                  str(oldloc[0].dpid), oldloc[1],
                  str(loc[0].dpid), loc[1])
        if packet.src.is_multicast == False:
          mac_map[packet.src] = loc # Learn position for ethaddr
          log.debug("Learned %s at %s.%i", packet.src, loc[0], loc[1])
      elif packet.dst.is_multicast == False:
        # New place is a switch-to-switch port!
        # Hopefully, this is a packet we're flooding because we didn't
        # know the destination, and not because it's somehow not on a
        # path that we expect it to be on.
        # If spanning_tree is running, we might check that this port is
        # on the spanning tree (it should be).
        if packet.dst in mac_map:
          # Unfortunately, we know the destination.  It's possible that
          # we learned it while it was in flight, but it's also possible
          # that something has gone wrong.
          log.warning("Packet from %s to known destination %s arrived "
                      "at %s.%i without flow", packet.src, packet.dst,
                      str(self.dpid), event.port)


    if packet.dst.is_multicast:
      log.debug("Flood multicast from %s", packet.src)
      flood()
    else:
      if packet.dst not in mac_map:
        log.debug("%s unknown -- flooding" % (packet.dst,))
        flood()
      else:
        dest = mac_map[packet.dst]
        match = of.ofp_match.from_packet(packet,spec_frags= True)
        self.install_path(dest[0], dest[1], match, event)

  def disconnect (self):
    if self.connection is not None:
      log.debug("Disconnect %s" % (self.connection,))
      self.connection.removeListeners(self._listeners)
      self.connection = None
      self._listeners = None

  def connect (self, connection):
    if self.dpid is None:
      self.dpid = connection.dpid
    assert self.dpid == connection.dpid
    if self.ports is None:
      self.ports = connection.features.ports
      self.__init_port_congestion_and_port_to_path()
    self.disconnect()
    log.debug("Connect %s" % (connection,))
    self.connection = connection
    self._listeners = self.listenTo(connection)
    self._connected_at = time.time()

  @property
  def is_holding_down (self):
    if self._connected_at is None: return True
    if time.time() - self._connected_at > FLOOD_HOLDDOWN:
      return False
    return True

  def _handle_ConnectionDown (self, event):
    self.disconnect()


class l2_multi (EventMixin):

  _eventMixin_events = set([
    PathInstalled,
  ])

  def __init__ (self):
    # Listen to dependencies (specifying priority 0 for openflow)
    core.listen_to_dependencies(self, listen_args={'openflow':{'priority':0}})

  def _handle_openflow_discovery_LinkEvent (self, event):
    def flip (link):
      return Discovery.Link(link.dpid2, link.port2, link.dpid1, link.port1, link.link_type,link.available)

    l = event.link
    sw1 = switches[l.dpid1]
    sw2 = switches[l.dpid2]

    # Invalidate all flows and path info.
    # For link adds, this makes sure that if a new link leads to an
    # improved path, we use it.
    # For link removals, this makes sure that we don't use a
    # path that may have been broken.
    #NOTE: This could be radically improved! (e.g., not *ALL* paths break)
    '''clear = of.ofp_flow_mod(command=of.OFPFC_DELETE)
    for sw in switches.itervalues():
      if sw.connection is None: continue
      sw.connection.send(clear)'''
    path_map.clear()
    if l.link_type is 'lldp':

      if event.removed:
        # This link no longer okay
        if sw2 in adjacency[sw1]: del adjacency[sw1][sw2]
        if sw1 in adjacency[sw2]: del adjacency[sw2][sw1]

        # But maybe there's another way to connect these...
        for ll in core.openflow_discovery.adjacency:
          if ll.dpid1 == l.dpid1 and ll.dpid2 == l.dpid2:
            if flip(ll) in core.openflow_discovery.adjacency:
              # Yup, link goes both ways
              adjacency[sw1][sw2] = ll.port1
              adjacency[sw2][sw1] = ll.port2
              # Fixed -- new link chosen to connect these
              break
      else:
        # If we already consider these nodes connected, we can
        # ignore this link up.
        # Otherwise, we might be interested...
        if adjacency[sw1][sw2] is None:
          # These previously weren't connected.  If the link
          # exists in both directions, we consider them connected now.
          if flip(l) in core.openflow_discovery.adjacency:
            # Yup, link goes both ways -- connected!
            adjacency[sw1][sw2] = l.port1
            adjacency[sw2][sw1] = l.port2
    elif l.link_type is 'broadcast':
      self.clear_the_previous()
      self.update_clouds_in_broadcast()


    # If we have learned a MAC on this port which we now know to
    # be connected to a switch, unlearn it.
    bad_macs = set()
    for mac,(sw,port) in mac_map.iteritems():
      if sw is sw1 and port == l.port1: bad_macs.add(mac)
      if sw is sw2 and port == l.port2: bad_macs.add(mac)
    for mac in bad_macs:
      log.debug("Unlearned %s", mac)
      del mac_map[mac]

  def _handle_openflow_ConnectionUp (self, event):
    sw = switches.get(event.dpid)
    if sw is None:
      # New switch
      sw = Switch()
      switches[event.dpid] = sw
      sw.connect(event.connection)
    else:
      sw.connect(event.connection)

  def _handle_openflow_BarrierIn (self, event):
    wp = waiting_paths.pop((event.dpid,event.xid), None)
    if not wp:
      #log.info("No waiting packet %s,%s", event.dpid, event.xid)
      return
    #log.debug("Notify waiting packet %s,%s", event.dpid, event.xid)
    wp.notify(event)

  def _handle_openflow_PortStats(self, event):

    sw = switches[event.dpid]
    port = event.ofp.port_no
    if _is_edge_port_in_topo(sw,port):
      return
    sw.port_congestion[port] = event.ofp.tx_congestion
    sw.update_congestion_path(port)


  def clear_the_previous(self):

    for sw_cloud in broadcast_adj:
      del adjacency[sw_cloud[0]][sw_cloud[1]]
      del adjacency[sw_cloud[1]][sw_cloud[0]]


    for cloud in clouds:
      del switches[cloud]
    clouds.clear()
    broadcast_adj.clear()



  def update_clouds_in_broadcast(self):
    from pox.openflow.spanning_tree import node_to_be_down
    g = nx.Graph()
    for link in generator_for_link('broadcast'):
      if link.available is True:
        g.add_edge((link.dpid1,link.port1), (link.dpid2,link.port2))

    for clique in nx.find_cliques(g):
      if frozenset(clique) in node_to_be_down.keys():
        clique.remove(node_to_be_down[frozenset(clique)])
      cloud_id = frozenset(clique)
      cloud = Cloud(cloud_id)
      cloud.ports.extend(clique)
      clouds[cloud_id] = cloud
      switches[cloud_id]= cloud
      for sw in clique:
        sw_dpid = switches[sw[0]]
        sw_port = sw[1]
        adjacency[cloud][sw_dpid] = 0
        adjacency[sw_dpid][cloud] = sw_port
        broadcast_adj.add((sw_dpid,cloud))


class Cloud(Switch):

  def __init__(self,id):
    super(Cloud,self).__init__()
    self.sw = set()
    self.dpid = id
    self.ports = []

  def __repr__(self):
    return str(self.dpid)





def launch ():
  core.registerNew(l2_multi)

  timeout = min(max(PATH_SETUP_TIME, 5) * 2, 15)
  Timer(timeout, WaitingPath.expire_waiting_paths, recurring=True)

#!/usr/bin/env python
#-*- coding: utf-8 -*-
from __future__ import print_function

import itertools as it, operator as op, functools as ft
from xml.etree import ElementTree
from io import BytesIO
from datetime import datetime
from collections import namedtuple, defaultdict
import os, sys, logging, re, glob, errno, socket

from nfct_cffi import NFCT


FlowData = namedtuple('FlowData', 'ts proto src dst sport dport')

def parse_event(ev_xml):
	etree = ElementTree.parse(BytesIO(ev_xml))

	flow = next(etree.iter())
	assert flow.attrib['type'] == 'new', ev_xml

	ts = flow.find('when')
	ts = datetime(*(int(ts.find(k).text) for k in ['year', 'month', 'day', 'hour', 'min', 'sec']))

	flow_data = dict()
	for meta in flow.findall('meta'):
		if meta.attrib['direction'] in ['original', 'reply']:
			l3, l4 = it.imap(meta.find, ['layer3', 'layer4'])
			proto = l3.attrib['protoname'], l4.attrib['protoname']
			if proto[1] not in ['tcp', 'udp']: return
			proto = '{}/{}'.format(*proto)
			src, dst = (l3.find(k).text for k in ['src', 'dst'])
			sport, dport = (int(l4.find(k).text) for k in ['sport', 'dport'])
			flow_data[meta.attrib['direction']] = FlowData(ts, proto, src, dst, sport, dport)

	# Fairly sure all new flows should be symmetrical, check that
	fo, fr = op.itemgetter('original', 'reply')(flow_data)
	assert fo.proto == fr.proto\
		and fo.src == fr.dst and fo.dst == fr.src\
		and fo.sport == fr.dport and fo.dport == fr.sport,\
		flow_data

	return flow_data['original']


def parse_ipv4(enc):
	return socket.inet_ntop(socket.AF_INET, ''.join(reversed(enc.decode('hex'))))

def parse_ipv6( enc,
		_endian=op.itemgetter(*(slice(n*2, (n+1)*2) for n in [6, 7, 4, 5, 2, 3, 0, 1])) ):
	return socket.inet_ntop( socket.AF_INET6,
		''.join(_endian(''.join(reversed(enc.decode('hex'))))) )

def get_table_sk( proto,
		_line_proc=op.itemgetter(1, 2, 9),
		_proto_conns={
			'ipv4/tcp': '/proc/net/tcp', 'ipv6/tcp': '/proc/net/tcp6',
			'ipv4/udp': '/proc/net/udp', 'ipv6/udp': '/proc/net/udp6' } ):
	_ntoa = ft.partial(parse_ipv4 if proto.startswith('ipv4/') else parse_ipv6)
	with open(_proto_conns[proto]) as src:
		next(src)
		for line in src:
			a, b, sk = _line_proc(line.split())
			k = (ep.split(':', 1) for ep in [a, b])
			k = tuple(sorted((_ntoa(ip), int(p, 16)) for ip,p in k))
			yield k, sk


def get_table_links():
	links = list()
	for path in glob.iglob('/proc/[0-9]*/fd/[0-9]*'):
		try: link = os.readlink(path)
		except OSError as err:
			if err.errno != errno.ENOENT: raise
			continue
		links.append((path, link))
	for path, link in links:
		match = re.search(r'^socket:\[([^]]+)\]$', link)
		if not match: continue
		yield match.group(1), int(re.search(r'^/proc/(\d+)/', path).group(1))


def pid_info(pid, entry):
	return open('/proc/{}/{}'.format(pid, entry)).read()

class FlowInfo(namedtuple('FlowInfo', 'pid uid gid cmdline service')):
	__slots__ = tuple()

	def __new__(cls, pid=None):
		uid = gid = cmdline = service = '-'
		if pid is not None:
			try:
				cmdline, service = (pid_info(pid, k) for k in ['cmdline', 'cgroup'])
				stat = os.stat('/proc/{}'.format(pid))
				uid, gid = op.attrgetter('st_uid', 'st_gid')(stat)
			except OSError:
				if err.errno != errno.ENOENT: raise
			if cmdline != '-': cmdline = cmdline.replace('\0', ' ').strip()
			if service != '-':
				for line in service.splitlines():
					line = line.split(':')
					if not re.search(r'^name=', line[1]): continue
					service = line[2]
					break
		return super(FlowInfo, cls).__new__(cls, pid or '?', uid, gid, cmdline, service)


def get_flow_info(flow, _nx=FlowInfo(), _cache=dict()):
	_cache = _cache.setdefault(flow.proto, defaultdict(dict))

	cache = _cache['sk']
	ip_key = tuple(sorted([(flow.src, flow.sport), (flow.dst, flow.dport)]))
	if ip_key not in cache:
		cache.clear()
		cache.update(get_table_sk(flow.proto))
	if ip_key not in cache:
		log.info('Failed to find connection for {}'.format(ip_key))
		return _nx
	sk = cache[ip_key]

	cache = _cache['links']
	if sk not in cache:
		cache.clear()
		cache.update(get_table_links())
	if sk not in cache:
		log.info('Failed to find pid for {}'.format(ip_key))
		return _nx
	pid = cache[sk]

	cache = _cache['info']
	try: pid_ts = int(pid_info(pid, 'stat').split()[21])
	except OSError:
		log.info('Failed to query pid info for {}'.format(ip_key))
		return _nx
	else:
		if pid in cache:
			info_ts, info = cache[pid]
			if pid_ts != info_ts: del cache[pid] # check starttime to detect pid rotation
		if pid not in cache:
			cache[pid] = pid_ts, FlowInfo(pid)
		info_ts, info = cache[pid]

	return info


def main(argv=None):
	import argparse
	parser = argparse.ArgumentParser(description='conntrack event logging/audit tool.')
	parser.add_argument('-p', '--protocol',
		help='Regexp (python) filter to match "ev.proto". Examples: ipv4, tcp, ipv6/udp.')
	parser.add_argument('-t', '--format-ts', default='%s',
		help='Timestamp format, as for datetime.strftime() (default: %(default)s).')
	parser.add_argument('-f', '--format',
		default='{ts}: {ev.proto} {ev.src}/{ev.sport} > {ev.dst}/{ev.dport}'
			' :: {info.pid} {info.uid}:{info.gid} {info.service} :: {info.cmdline}',
		help='Output format for each new flow, as for str.format() (default: %(default)s).')
	parser.add_argument('--debug',
		action='store_true', help='Verbose operation mode.')
	opts = parser.parse_args(argv or sys.argv[1:])

	opts.format += '\n'

	import logging
	logging.basicConfig(level=logging.DEBUG if opts.debug else logging.WARNING)
	global log
	log = logging.getLogger()

	nfct = NFCT()
	src = nfct.generator(events=nfct.libnfct.NFNLGRP_CONNTRACK_NEW)
	next(src) # netlink fd

	log.debug('Started logging')
	for ev_xml in src:
		try: ev = parse_event(ev_xml)
		except:
			log.error('Failed to parse event data: {}'.format(ev_xml))
			continue
		if not ev: continue
		if opts.protocol and not re.search(opts.protocol, ev.proto): continue
		sys.stdout.write(opts.format.format( ev=ev,
			ts=ev.ts.strftime(opts.format_ts), info=get_flow_info(ev) ))
		sys.stdout.flush()


if __name__ == '__main__': sys.exit(main())

#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Environments concepts.

For internal use only. No backwards compatibility guarantees."""

from __future__ import absolute_import

import json

from google.protobuf import message

from apache_beam.portability import common_urns
from apache_beam.portability import python_urns
from apache_beam.portability.api import beam_runner_api_pb2
from apache_beam.portability.api import endpoints_pb2
from apache_beam.utils import proto_utils

__all__ = ['Environment',
           'DockerEnvironment', 'ProcessEnvironment', 'ExternalEnvironment',
           'EmbeddedPythonEnvironment', 'EmbeddedPythonGrpcEnvironment',
           'SubprocessSDKEnvironment', 'RunnerAPIEnvironmentHolder']


class Environment(object):
  """Abstract base class for environments.

  Represents a type and configuration of environment.
  Each type of Environment should have a unique urn.

  For internal use only. No backwards compatibility guarantees.
  """

  _known_urns = {}
  _urn_to_env_cls = {}

  def to_runner_api_parameter(self, context):
    raise NotImplementedError

  @classmethod
  def register_urn(cls, urn, parameter_type, constructor=None):

    def register(constructor):
      if isinstance(constructor, type):
        constructor.from_runner_api_parameter = register(
            constructor.from_runner_api_parameter)
        # register environment urn to environment class
        cls._urn_to_env_cls[urn] = constructor
        return constructor

      else:
        cls._known_urns[urn] = parameter_type, constructor
        return staticmethod(constructor)

    if constructor:
      # Used as a statement.
      register(constructor)
    else:
      # Used as a decorator.
      return register

  @classmethod
  def get_env_cls_from_urn(cls, urn):
    return cls._urn_to_env_cls[urn]

  def to_runner_api(self, context):
    urn, typed_param = self.to_runner_api_parameter(context)
    return beam_runner_api_pb2.Environment(
        urn=urn,
        payload=typed_param.SerializeToString()
        if isinstance(typed_param, message.Message)
        else typed_param if (isinstance(typed_param, bytes) or
                             typed_param is None)
        else typed_param.encode('utf-8')
    )

  @classmethod
  def from_runner_api(cls, proto, context):
    if proto is None or not proto.urn:
      return None
    parameter_type, constructor = cls._known_urns[proto.urn]

    try:
      return constructor(
          proto_utils.parse_Bytes(proto.payload, parameter_type),
          context)
    except Exception:
      if context.allow_proto_holders:
        return RunnerAPIEnvironmentHolder(proto)
      raise

  @classmethod
  def from_options(cls, options):
    """Creates an Environment object from PipelineOptions.

    Args:
      options: The PipelineOptions object.
    """
    raise NotImplementedError


@Environment.register_urn(common_urns.environments.DOCKER.urn,
                          beam_runner_api_pb2.DockerPayload)
class DockerEnvironment(Environment):

  def __init__(self, container_image=None):
    from apache_beam.runners.portability.portable_runner import PortableRunner

    if container_image:
      self.container_image = container_image
    else:
      self.container_image = PortableRunner.default_docker_image()

  def __eq__(self, other):
    return self.__class__ == other.__class__ \
           and self.container_image == other.container_image

  def __ne__(self, other):
    # TODO(BEAM-5949): Needed for Python 2 compatibility.
    return not self == other

  def __hash__(self):
    return hash((self.__class__, self.container_image))

  def __repr__(self):
    return 'DockerEnvironment(container_image=%s)' % self.container_image

  def to_runner_api_parameter(self, context):
    return (common_urns.environments.DOCKER.urn,
            beam_runner_api_pb2.DockerPayload(
                container_image=self.container_image))

  @staticmethod
  def from_runner_api_parameter(payload, context):
    return DockerEnvironment(container_image=payload.container_image)

  @classmethod
  def from_options(cls, options):
    return cls(container_image=options.environment_config)


@Environment.register_urn(common_urns.environments.PROCESS.urn,
                          beam_runner_api_pb2.ProcessPayload)
class ProcessEnvironment(Environment):

  def __init__(self, command, os='', arch='', env=None):
    self.command = command
    self.os = os
    self.arch = arch
    self.env = env or {}

  def __eq__(self, other):
    return self.__class__ == other.__class__ \
      and self.command == other.command and self.os == other.os \
      and self.arch == other.arch and self.env == other.env

  def __ne__(self, other):
    # TODO(BEAM-5949): Needed for Python 2 compatibility.
    return not self == other

  def __hash__(self):
    return hash((self.__class__, self.command, self.os, self.arch,
                 frozenset(self.env.items())))

  def __repr__(self):
    repr_parts = ['command=%s' % self.command]
    if self.os:
      repr_parts.append('os=%s'% self.os)
    if self.arch:
      repr_parts.append('arch=%s' % self.arch)
    repr_parts.append('env=%s' % self.env)
    return 'ProcessEnvironment(%s)' % ','.join(repr_parts)

  def to_runner_api_parameter(self, context):
    return (common_urns.environments.PROCESS.urn,
            beam_runner_api_pb2.ProcessPayload(
                os=self.os,
                arch=self.arch,
                command=self.command,
                env=self.env))

  @staticmethod
  def from_runner_api_parameter(payload, context):
    return ProcessEnvironment(command=payload.command, os=payload.os,
                              arch=payload.arch, env=payload.env)

  @classmethod
  def from_options(cls, options):
    config = json.loads(options.environment_config)
    return cls(config.get('command'), os=config.get('os', ''),
               arch=config.get('arch', ''), env=config.get('env', ''))


@Environment.register_urn(common_urns.environments.EXTERNAL.urn,
                          beam_runner_api_pb2.ExternalPayload)
class ExternalEnvironment(Environment):

  def __init__(self, url, params=None):
    self.url = url
    self.params = params

  def __eq__(self, other):
    return self.__class__ == other.__class__ and self.url == other.url \
      and self.params == other.params

  def __ne__(self, other):
    # TODO(BEAM-5949): Needed for Python 2 compatibility.
    return not self == other

  def __hash__(self):
    params = self.params
    if params is not None:
      params = frozenset(self.params.items())
    return hash((self.__class__, self.url, params))

  def __repr__(self):
    return 'ExternalEnvironment(url=%s,params=%s)' % (self.url, self.params)

  def to_runner_api_parameter(self, context):
    return (common_urns.environments.EXTERNAL.urn,
            beam_runner_api_pb2.ExternalPayload(
                endpoint=endpoints_pb2.ApiServiceDescriptor(url=self.url),
                params=self.params
            ))

  @staticmethod
  def from_runner_api_parameter(payload, context):
    return ExternalEnvironment(payload.endpoint.url,
                               params=payload.params or None)

  @classmethod
  def from_options(cls, options):
    def looks_like_json(environment_config):
      import re
      return re.match(r'\s*\{.*\}\s*$', environment_config)

    if looks_like_json(options.environment_config):
      config = json.loads(options.environment_config)
      url = config.get('url')
      if not url:
        raise ValueError('External environment endpoint must be set.')
      params = config.get('params')
    else:
      url = options.environment_config
      params = None

    return cls(url, params=params)


@Environment.register_urn(python_urns.EMBEDDED_PYTHON, None)
class EmbeddedPythonEnvironment(Environment):

  def __eq__(self, other):
    return self.__class__ == other.__class__

  def __ne__(self, other):
    # TODO(BEAM-5949): Needed for Python 2 compatibility.
    return not self == other

  def __hash__(self):
    return hash(self.__class__)

  def to_runner_api_parameter(self, context):
    return python_urns.EMBEDDED_PYTHON, None

  @staticmethod
  def from_runner_api_parameter(unused_payload, context):
    return EmbeddedPythonEnvironment()

  @classmethod
  def from_options(cls, options):
    return cls()


@Environment.register_urn(python_urns.EMBEDDED_PYTHON_GRPC, bytes)
class EmbeddedPythonGrpcEnvironment(Environment):

  def __init__(self, num_workers=None, state_cache_size=None):
    self.num_workers = num_workers
    self.state_cache_size = state_cache_size

  def __eq__(self, other):
    return self.__class__ == other.__class__ \
           and self.num_workers == other.num_workers \
           and self.state_cache_size == other.state_cache_size

  def __ne__(self, other):
    # TODO(BEAM-5949): Needed for Python 2 compatibility.
    return not self == other

  def __hash__(self):
    return hash((self.__class__, self.num_workers, self.state_cache_size))

  def __repr__(self):
    repr_parts = []
    if not self.num_workers is None:
      repr_parts.append('num_workers=%d' % self.num_workers)
    if not self.state_cache_size is None:
      repr_parts.append('state_cache_size=%d' % self.state_cache_size)
    return 'EmbeddedPythonGrpcEnvironment(%s)' % ','.join(repr_parts)

  def to_runner_api_parameter(self, context):
    if self.num_workers is None and self.state_cache_size is None:
      payload = b''
    elif self.num_workers is not None and self.state_cache_size is not None:
      payload = b'%d,%d' % (self.num_workers, self.state_cache_size)
    else:
      # We want to make sure that the environment stays the same through the
      # roundtrip to runner api, so here we don't want to set default for the
      # other if only one of num workers or state cache size is set
      raise ValueError('Must provide worker num and state cache size.')
    return python_urns.EMBEDDED_PYTHON_GRPC, payload

  @staticmethod
  def from_runner_api_parameter(payload, context):
    if payload:
      num_workers, state_cache_size = payload.decode('utf-8').split(',')
      return EmbeddedPythonGrpcEnvironment(
          num_workers=int(num_workers),
          state_cache_size=int(state_cache_size))
    else:
      return EmbeddedPythonGrpcEnvironment()

  @classmethod
  def from_options(cls, options):
    if options.environment_config:
      num_workers, state_cache_size = options.environment_config.split(',')
      return cls(num_workers=num_workers, state_cache_size=state_cache_size)
    else:
      return cls()


@Environment.register_urn(python_urns.SUBPROCESS_SDK, bytes)
class SubprocessSDKEnvironment(Environment):

  def __init__(self, command_string):
    self.command_string = command_string

  def __eq__(self, other):
    return self.__class__ == other.__class__ \
           and self.command_string == other.command_string

  def __ne__(self, other):
    # TODO(BEAM-5949): Needed for Python 2 compatibility.
    return not self == other

  def __hash__(self):
    return hash((self.__class__, self.command_string))

  def __repr__(self):
    return 'SubprocessSDKEnvironment(command_string=%s)' % self.container_string

  def to_runner_api_parameter(self, context):
    return python_urns.SUBPROCESS_SDK, self.command_string.encode('utf-8')

  @staticmethod
  def from_runner_api_parameter(payload, context):
    return SubprocessSDKEnvironment(payload.decode('utf-8'))

  @classmethod
  def from_options(cls, options):
    return cls(options.environment_config)


class RunnerAPIEnvironmentHolder(Environment):

  def __init__(self, proto):
    self.proto = proto

  def to_runner_api(self, context):
    return self.proto

  def __eq__(self, other):
    return self.__class__ == other.__class__ and self.proto == other.proto

  def __ne__(self, other):
    # TODO(BEAM-5949): Needed for Python 2 compatibility.
    return not self == other

  def __hash__(self):
    return hash((self.__class__, self.proto))

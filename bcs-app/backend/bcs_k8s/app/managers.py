# -*- coding: utf-8 -*-
#
# Tencent is pleased to support the open source community by making 蓝鲸智云PaaS平台社区版 (BlueKing PaaS Community Edition) available.
# Copyright (C) 2017-2019 THL A29 Limited, a Tencent company. All rights reserved.
# Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://opensource.org/licenses/MIT
#
# Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
import json
import logging

from django.utils.crypto import get_random_string
from django.db import models
from rest_framework.serializers import ValidationError
from django.db.models import Max

from backend.bcs_k8s.helm.constants import TEMPORARY_APP_ID
from backend.bcs_k8s.helm.models import ChartVersionSnapshot, ChartRelease
from .deployer import AppDeployer
from backend.activity_log import client

logger = logging.getLogger(__name__)


class AppManager(models.Manager):
    def new_unique_ns(self):
        return self._max_unique_ns() + 1

    def _max_unique_ns(self):
        max_value = self.model.objects.all().aggregate(Max('unique_ns'))
        return max_value["unique_ns__max"]

    def record_initialize_app(self, access_token, project_id, cluster_id, namespace_id, namespace,
                              chart_version, answers, customs, creator, updator, valuefile=None,
                              name=None, unique_ns=0, sys_variables=None):
        # operation record
        extra = json.dumps(dict(
            access_token=access_token,
            name=name,
            project_id=project_id,
            cluster_id=cluster_id,
            namespace_id=namespace_id,
            namespace=namespace,
            chart_version=chart_version.id,
            answers=answers,
            customs=customs,
            valuefile=valuefile,
            creator=creator,
            updator=updator,
            unique_ns=unique_ns,
            sys_variables=sys_variables,
        ))
        logger_client = client.UserActivityLogClient(
            project_id=project_id,
            user=creator,
            activity_type="add",
            resource_type="helm_app",
            resource=chart_version.name,
            resource_id=chart_version.id,
            extra=extra,
            description="创建Helm App, 实例化模板集[{chart_name}:{template_id}], 集群[{cluster_id}], 命名空间[{namespace}]".format(
                template_id=chart_version.id,
                chart_name=chart_version.chart.name,
                namespace=namespace,
                cluster_id=cluster_id,
            )
        )
        logger_client.log(activity_status="busy")
        return logger_client

    def initialize_app(self, access_token, project_id, cluster_id, namespace_id, namespace,
                       chart_version, answers, customs, creator, updator, valuefile=None, name=None,
                       unique_ns=0, deploy_options=None, sys_variables=None):
        # initial app from chart
        # `namespace_id` indicate the id of namespace, and is unique in whole paas-cc, used for filter app.
        # `namespace` is basestring type and can convert by namespace_id ,
        # as visit paas-cc interface need a access_token,
        # we keep it for rendering a chart.
        # `values` is designed for keep namespace values when release a chart.

        log_client = self.record_initialize_app(
            access_token=access_token,
            name=name,
            project_id=project_id,
            cluster_id=cluster_id,
            namespace_id=namespace_id,
            namespace=namespace,
            chart_version=chart_version,
            answers=answers,
            customs=customs,
            valuefile=valuefile,
            creator=creator,
            updator=updator,
            unique_ns=unique_ns,
            sys_variables=sys_variables,
        )
        try:
            app = self.initialize_app_core(
                access_token=access_token,
                project_id=project_id,
                cluster_id=cluster_id,
                namespace_id=namespace_id,
                namespace=namespace,
                chart_version=chart_version,
                answers=answers,
                customs=customs,
                creator=creator,
                updator=updator,
                valuefile=valuefile,
                name=name,
                unique_ns=unique_ns,
                deploy_options=deploy_options,
                sys_variables=sys_variables
            )
        except Exception as e:
            logger.exception("initialize_app_core unexpected error: %s", e)
            log_client.update_log(activity_status="failed")
            raise e

        if deploy_options is None:
            deploy_options = dict()

        app.first_deploy(
            access_token=access_token,
            deploy_options=deploy_options,
            activity_log_id=log_client.activity_log.id,
        )
        return app

    def initialize_app_core(self, access_token, project_id, cluster_id, namespace_id, namespace,
                            chart_version, answers, customs, creator, updator, valuefile=None, name=None,
                            unique_ns=0, deploy_options=None, sys_variables=None):
        if not sys_variables:
            sys_variables = {}

        # initial app from chart
        if unique_ns == 0 and self.filter(namespace_id=namespace_id, chart=chart_version.chart).exists():
            raise ValidationError(
                "chart %s has been initialized in namespace %s" % (chart_version.chart.name, namespace))

        customs = customs if customs else list()
        assert isinstance(answers, list), ValidationError("answers can only be list")
        assert isinstance(customs, list), ValidationError("customs can only be list")
        valuefile = "" if not valuefile else valuefile
        snapshot = ChartVersionSnapshot.objects.make_snapshot(chart_version)
        release = ChartRelease.objects.create(
            repository=chart_version.chart.repository,
            chart=chart_version.chart,
            chartVersionSnapshot=snapshot,
            answers=answers,
            customs=customs,
            valuefile=valuefile,
            app_id=TEMPORARY_APP_ID,
        )

        if name is None:
            name = "{namespace}-{randstr}".format(
                namespace=namespace,
                randstr=get_random_string(5)
            ).lower()

        app = self.create(
            project_id=project_id,
            cluster_id=cluster_id,
            chart=chart_version.chart,
            release=release,
            namespace=namespace,
            namespace_id=namespace_id,
            name=name,
            creator=creator,
            updator=updator,
            unique_ns=unique_ns,
            sys_variables=sys_variables,
            version=chart_version.version
        )
        release.app_id = app.id
        release.save(update_fields=["app_id"])

        return app

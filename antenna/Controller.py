# Copyright 2016 Morgan McDermott & Blake Allen"""
"""

The Controller class coordinates the creation of Sources and Transformers.

"""
import os
import os.path
import re
import json
import time
import boto3
import shutil
import datetime
import importlib
from zipfile import ZipFile, ZIP_DEFLATED
from threading import Thread
import concurrent
from concurrent.futures import ThreadPoolExecutor

import antenna.Sources as Sources
import antenna.Transformers as Transformers
import antenna.Filters as Filters
import antenna.Storage as Storage
import antenna.AWSManager as AWSManager
import antenna.ResourceManager as ResourceManager
import botocore

import redleader.util

sourceClassMap = {
    'RSSFeedSource': Sources.RSSFeedSource,
    "NewspaperLibSource": Sources.NewspaperLibSource
}

transformerClassMap = {
    "IdentityTransformer": Transformers.IdentityTransformer,
    "NewspaperLibScraper": Transformers.NewspaperLibScraper
}

storageClassMap = {
    "DynamoDBStorage": Storage.DynamoDBStorage
}

filterClassMap = {
    "UniqueDynamoDBFilter": Filters.UniqueDynamoDBFilter
}

class Controller(object):
    def __init__(self, config, source_path=None, aws_profile=None, no_deploy=False):
        self._defaults = {
            'local_controller': False,
            'local_jobs': False,
            'local_queue': False,
            'controller_schedule': 5, # Run the controller every N minutes
            'aws_region': 'us-west-1',
            'runtime': 250 # Maximum runtime defaults to 60s. This applies to transformer
                          # queue jobs only (typically the running portion)

        }

        self._source_path = source_path
        if self._source_path is None:
            self._source_path = os.path.dirname(os.path.abspath(__file__))
        print("Controller initialized with source path %s (%s)" %
              (self._source_path, str(source_path)))


        for key in config:
            setattr(self, key, config[key])

        for key in self._defaults:
            if key not in config:
                setattr(self, key, self._defaults[key])

        self._aws_profile = aws_profile
        self._aws_manager = AWSManager.AWSManager(aws_profile=aws_profile,
                                                  aws_region=self.aws_region)
        self._sqs = self._aws_manager._session.resource('sqs')
        self._sqs_queues = {}

        self._resource_manager = ResourceManager.ResourceManager(self)
        #config = self.augment_config_with_dynamodb_data(config)
        self.validate_config(config)
        self.config = config
        self.local_queues = {}

        for key in config:
            setattr(self, key, config[key])

        # Deploy cluster on initialization
        if not no_deploy:
            self._cluster = self._resource_manager.create_resource_cluster()

        self._transformer_memory_size = 256
        self._source_memory_size = 256
        self._controller_memory_size = 256

        print("Controller setup complete")

    def validate_config(self, config):
        required_keys = ['sources', 'transformers', 'project_name']
        for key in required_keys:
            if key not in config:
                raise Exception('Config must have key %s' % key)

    def augment_config_with_dynamodb_data(self, config):
        source_list_table_name = self._resource_manager.dynamo_table_name("source_list")
        client = self._aws_manager.get_client('dynamodb')
        print("Augmenting config with dynamodb data from table " + source_list_table_name)
        try:
            resp = client.scan(TableName=source_list_table_name)
        except Exception as e:
            if "ResourceNotFound" in str(e):
                print("Dynamic DynamoDB source table does not yet exist")
                return config
            else:
                raise e

        print("Augmenting config with dynamodb data")
        items = {}
        for item in resp['Items']:
            d = Storage.DynamoDBStorage.from_dynamo_dict(item)
            items[d['uuid']] = d

        last_evaluated_key = resp.get('LastEvaluatedKey', None)
        while last_evaluated_key is not None:
            for item in resp['Items']:
                d = Storage.DynamoDBStorage.from_dynamo_dict(item)
                items[d['uuid']] = d

            resp = client.scan(
                TableName=source_list_table_name,
                ExclusiveStartKey=last_evaluated_key)

            last_evaluated_key = resp.get('LastEvaluatedKey', None)

        print("Retrieved " + str(len(items)) + " items from dynamodb")
        new_sources = [x for x in config['sources']]
        for item in items:
            new_sources.append(items[item])
        config['sources'] = new_sources
        return config

    def create_resources(self, force_update=False):
        """
        Uses the RedLeader cluster to deploy a cloud formation template
        for all resources.
        """
        try:
            if force_update:
                self._cluster.blocking_delete(verbose=True)
            self._cluster.blocking_deploy(verbose=True)
        except botocore.exceptions.ClientError as e:
            if "AlreadyExists" not in "%s" % e:
                raise e
            print("Stack already exists. Updating.")
            try:
                self._cluster.blocking_update(verbose = True)
            except botocore.exceptions.ClientError as e:
                if "No updates" not in "%s" % e:
                    raise e
                print("No update necessary.")

    def transformer_lambda_name(self, config):
        return "%sTransformer%s" % (self.config['project_name'],
                                           config['type'].split(".")[-1])

    def source_lambda_name(self, config):
        return "%sSource%s" % (self.config['project_name'],
                                      config['type'].replace(".", "_"))

    def controller_lambda_name(self):
        return "%sController" % (self.config['project_name'])

    def source_controller_lambda_name(self):
        return "%sSourceController" % (self.config['project_name'])

    def transformer_controller_lambda_name(self):
        return "%sTransformerController" % (self.config['project_name'])

    def aggregate_transformer_lambda_name(self):
        return "%sAggregateTransformer" % (self.config['project_name'])

    def aggregate_controller_lambda_name(self):
        return "%sAggregateController" % (self.config['project_name'])

    def config_bucket_name(self):
        return "%sconfigbucket" % redleader.util.sanitize((self.config['project_name']).lower())

    def create_lambda_functions(self):

        # Create lambda functions for each source type
        zipfilepath = create_lambda_package(self._source_path)
        source_types = {}
        for config in self.config['sources']:
            if config['type'] in source_types:
                continue
            source_types[config['type']] = True
            create_lambda_function(self.source_lambda_name(config),
                                   self.get_lambda_role_arn(),
                                   self._aws_manager.get_client('lambda'),
                                   zipfilepath,
                                   "lambda_handlers.source_handler",
                                   memory_size=self._source_memory_size)

        # Create controller lambda function
        #zipfilepath = create_lambda_package(self._source_path)
        create_lambda_function(self.controller_lambda_name(),
                               self.get_lambda_role_arn(),
                               self._aws_manager.get_client('lambda'),
                               zipfilepath,
                               "lambda_handlers.controller_handler",
                               memory_size=self._controller_memory_size)

        # Create source controller lambda function
        #zipfilepath = create_lambda_package(self._source_path)
        create_lambda_function(self.source_controller_lambda_name(),
                               self.get_lambda_role_arn(),
                               self._aws_manager.get_client('lambda'),
                               zipfilepath,
                               "lambda_handlers.source_controller_handler",
                               memory_size=self._controller_memory_size)

        # Create transformer controller lambda function
        #zipfilepath = create_lambda_package(self._source_path)
        create_lambda_function(self.transformer_controller_lambda_name(),
                               self.get_lambda_role_arn(),
                               self._aws_manager.get_client('lambda'),
                               zipfilepath,
                               "lambda_handlers.transformer_controller_handler",
                               memory_size=self._controller_memory_size)

        # Create aggregate transformer lambda function
        #zipfilepath = create_lambda_package(self._source_path)
        create_lambda_function(self.aggregate_controller_lambda_name(),
                               self.get_lambda_role_arn(),
                               self._aws_manager.get_client('lambda'),
                               zipfilepath,
                               "lambda_handlers.aggregate_controller_handler",
                               memory_size=self._controller_memory_size)

        # Create aggregate transformer lambda function
        #zipfilepath = create_lambda_package(self._source_path)
        create_lambda_function(self.aggregate_transformer_lambda_name(),
                               self.get_lambda_role_arn(),
                               self._aws_manager.get_client('lambda'),
                               zipfilepath,
                               "lambda_handlers.aggregate_transformer_handler",
                               memory_size=self._controller_memory_size)




        # Create lambda functions for each transformer type
        transformer_types = {}
        for config in self.config['transformers']:
            if config['type'] in transformer_types:
                continue
            transformer_types[config['type']] = True
            create_lambda_function(self.transformer_lambda_name(config),
                                   self.get_lambda_role_arn(),
                                   self._aws_manager.get_client('lambda'),
                                   zipfilepath,
                                   "lambda_handlers.transformer_handler",
                                   memory_size=self._transformer_memory_size)

    def schedule_controller_lambda(self):
        self.schedule_lambda(self.controller_lambda_name(), self.controller_schedule)

    def schedule_source_controller_lambda(self):
        self.schedule_lambda(self.source_controller_lambda_name(), self.controller_schedule)

    def schedule_transformer_controller_lambda(self):
        self.schedule_lambda(self.aggregate_transformer_lambda_name(), "1")

    def schedule_aggregate_controller_lambda(self):
        self.schedule_lambda(self.aggregate_controller_lambda_name(), "5")

    def schedule_lambda(self, lambda_name, schedule_minutes):
        cloudwatch = self._aws_manager.get_client('events')
        lambdaclient = self._aws_manager.get_client('lambda')
        controller_function_arn = lambdaclient.get_function(
            FunctionName=lambda_name)['Configuration']['FunctionArn']
        rule_name = '%s%s' % (lambda_name, self.config['project_name'])
        res = cloudwatch.put_rule(
                Name=rule_name,
                ScheduleExpression='cron(0/%s * * * ? *)' % schedule_minutes,
        )
        try:
            res2 = lambdaclient.add_permission(
                Action='lambda:InvokeFunction',
                FunctionName=lambda_name,
                Principal='events.amazonaws.com',
                SourceArn=res['RuleArn'],
                StatementId='EventsInvoke%s' % lambda_name
            )
        except botocore.exceptions.ClientError as e:
            if "already exists" not in str(e):
                raise e
            else:
                print("Lambda permission for scheduled event to invoke function already exists.")
        cloudwatch.put_targets(
            Rule=rule_name,
            Targets=[{
                "Id": rule_name,
                "Arn": controller_function_arn
            }]
        )

    def get_sqs_queue(self, item_type):
        queue_name = self._resource_manager.queue_name(item_type)
        if item_type not in self._sqs_queues:
            url = self._aws_manager.get_client('sqs').get_queue_url(QueueName=queue_name)['QueueUrl']
            self._sqs_queues[item_type] = self._sqs.Queue(url)
        return self._sqs_queues[item_type]

    def get_aggregate_sqs_queue(self):
        queue_name = self._resource_manager.aggregate_queue_name()
        if queue_name not in self._sqs_queues:
            url = self._aws_manager.get_client('sqs').get_queue_url(QueueName=queue_name)['QueueUrl']
            self._sqs_queues[queue_name] = self._sqs.Queue(url)
        return self._sqs_queues[queue_name]

    def drain_queues(self):
        queues = {}
        for item_type in self.item_types():
            queue = self.get_sqs_queue(item_type)
            for message in queue.receive_messages():
                message.delete()

    def dequeue_local_item(self, item_type):
        """
        Dequeue an item for local use and testing (replacement for SQS queue)
        """
        if item_type not in self.local_queues:
            return None
        if len(self.local_queues[item_type]) == 0:
            return None
        return self.local_queues[item_type].pop()

    def queue_local_item(self, item):
        """
        Queue an item for local use and testing (replacement for SQS queue)
        """
        if item.item_type not in self.local_queues:
            self.local_queues[item.item_type] = []
        self.local_queues[item.item_type].append(item)

    def instantiate_source(self, config, skip_loading_state=False):
        if config['type'] not in sourceClassMap:
            if "." not in config['type']:
                raise Exception('Unknown source type %s ' % config['type'])
            source = self.import_source(config['type'], self._source_path)
            return source(self._aws_manager, config)

        source = sourceClassMap[config['type']](self._aws_manager, config)
        if not skip_loading_state:
            source.set_state(self.get_source_state(source))
        return source

    def import_source(self, classpath, source_path):
        """
        Imports a Source
        e.g.) given the path "source.MyCustomSource", imports:
              import MyCustomSource from sources
        """
        relpath = os.path.join(*(classpath.split('.')[:-1])) + '.py'
        fullpath = os.path.join(source_path, relpath)
        spec = importlib.util.spec_from_file_location(classpath.split(".")[-2], fullpath)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, classpath.split(".")[-1])

    def get_source_state(self, source):
        source_config_hash = source.config_hash()
        table_name = self._resource_manager.dynamo_table_name("source_state")
        ddb = self._aws_manager.get_client('dynamodb')

        # Attempt to retrieve old item.
        key = {"source_config_hash": {'S': source_config_hash}}
        try:
            res = ddb.get_item(TableName=table_name, Key=key)
        except botocore.exceptions.ClientError as e:
            print("Failed to retrieve source state: %s" % str(e))
            return None
        if 'Item' in res:
            print("Restoring source %s from retrieved state" % source_config_hash)
            return Storage.DynamoDBStorage.from_dynamo_dict(res['Item'])
        return None

    def update_source_state(self, source):
        source_config_hash = source.config_hash()
        source_state = source.get_state()
        source_state['source_config_hash'] = source_config_hash
        dynamo_source_state = Storage.DynamoDBStorage.dynamo_dict(source_state)
        table_name = self._resource_manager.dynamo_table_name("source_state")
        ddb = self._aws_manager.get_client('dynamodb')

        return ddb.put_item(TableName=table_name, Item=dynamo_source_state)

    def run_source_job(self, config):
        items = []
        source = self.instantiate_source(config)

        print("Source has new data? %s" % str(source.has_new_data()))
        for item in source.yield_items():
            if self.local_queue:
                self.queue_local_item(item)
            else:
                if not self.filter_item(self.config.get("source_filters", []), item):
                    print("Item filtered. Not storing nor queueing. (%s)" %
                          json.dumps(item.payload)[:64])
                else:
                    output_queue = self.get_sqs_queue(item.item_type)
                    output_queue.send_message(
                        MessageBody=json.dumps(item.payload, indent=4))
                    print("Created source item on queue %s (%s)" % (item.item_type, json.dumps(item.payload)[:64]))
                    items.append(item)
                    self.store_item(self.config.get("source_storage", []), item)
        self.update_source_state(source)
        return items


    def async_aggregate_transformers(self, items, batch_size=10):
        executor = ThreadPoolExecutor(max_workers=batch_size)
        future_to_item = {}
        for item in items:
            future = executor.submit(
                self.invoke_aggregate_transformer_lambda, item)
            future_to_item[future] = item
        print("Invoked.")
        for future in concurrent.futures.as_completed(future_to_item):
            item = future_to_item[future]
            try:
                data = future.result()
                print("Aggregate: ", data['StatusCode'])
            except Exception as exc:
                print('%r generated an exception: %s' % (url, exc))

    def async_aggregate_local_transformers(self, items, batch_size=10):
        executor = ThreadPoolExecutor(max_workers=20)
        future_to_item = {}
        for item in items:
            future = executor.submit(self.run_aggregate_transformer_job,
                    self.config, item, os.getcwd())
            future_to_item[future] = item
        print("Invoked.")
        for future in concurrent.futures.as_completed(future_to_item):
            item = future_to_item[future]
            try:
                data = future.result()
                print("Aggregate: ", item)
            except Exception as exc:
                print('%r generated an exception: %s' % (item, exc))

    def run_source_and_aggregate_transformer(self, config, local=True, batch_size=10):
        items = []
        source = self.instantiate_source(config)
        print("Source has new data? %s" % str(source.has_new_data()))
        print("Running locally: ", local)

        t0 = time.time()
        batch = []

        yielded = []
        for item in source.yield_items():
            yielded.append(item)

        for i, item in enumerate(yielded):
            if local:
                if len(batch) == batch_size or i == len(yielded) - 1:
                    t0 = time.time()
                    print("Running aggregate transformer locally")
                    self.async_aggregate_local_transformers(batch, batch_size)
                    print("Batch of len", len(batch), "transformed in ",
                          time.time() - t0, "(",
                          len(batch) * 1. / (time.time() - t0), " /s)")
                    batch = []
                                     #self.run_aggregate_transformer_job(
                                     #self.config, item, os.getcwd())
                else:
                    batch.append(item)
            else:
                if len(batch) == batch_size or i == len(yielded) - 1:
                    t0 = time.time()
                    self.async_aggregate_transformers(batch, batch_size)
                    print("Batch of len", len(batch), "transformed in ",
                          time.time() - t0, "(",
                          len(batch) * 1. / (time.time() - t0), " /s)")
                    batch = []
                else:
                    print("Source yielded item", json.dumps(item.payload)[:100])
                    batch.append(item)
                    continue
                #print("Invoking remote lambda")
                #ta = time.time()
                #self.invoke_aggregate_transformer_lambda(item)
                #print("Invocation time", time.time() - ta)
            td = time.time() - t0
            t0 = time.time()
            print("Finished processing aggregate transformer on item: ", td)
        self.update_source_state(source)
        return items

    def strip_sources(self, config):
        """
        Strip sources from a config so that its size doesn't exceed
        lambda's maximum limits
        """
        config_copy = {
            "sources": []
        }
        for k in config:
            if k != "sources":
                config_copy[k] = config[k]
        return config_copy

    def create_source_job(self, config):
        """
        Spawn a job for the given source config
        """
        source = self.instantiate_source(config)
        if source.has_new_data():
            print("Spawning job for source %s" % config['type'])
            print(json.dumps(config, indent=4))
            if True == self.local_jobs:
                self.run_source_job(config)
            else:
                event = {
                    'controller_config': json.dumps(self.strip_sources(self.config)),
                    'source_config': json.dumps(config)
                }
                response = self._aws_manager.get_client('lambda').invoke(
                    FunctionName=self.source_lambda_name(config),
                    InvocationType='Event',
                    Payload=json.dumps(event)
                )
        else:
            print("Source has no new data. Skipping.")

    def instantiate_transformer(self, config, source_path):
        if config['type'] not in transformerClassMap:
            if "." not in config['type']:
                raise Exception('Unknown transformer type %s ' % config['type'])
            transformer = self.import_transformer(config['type'], source_path)
            return transformer(self._aws_manager, config)
        return transformerClassMap[config['type']](self._aws_manager, config)

    def import_transformer(self, classpath, source_path):
        """
        Imports a Transformer
        e.g.) given the path "transformers.MyCustomTransformer", imports:
              import MyCustomTransformer from transformers
        """
        relpath = os.path.join(*(classpath.split('.')[:-1])) + '.py'
        fullpath = os.path.join(source_path, relpath)
        spec = importlib.util.spec_from_file_location(classpath.split(".")[-2], fullpath)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, classpath.split(".")[-1])

    def import_filter(self, classpath, source_path):
        return self.import_transformer(classpath, source_path)

    def instantiate_filter(self, config):
        if config['type'] not in filterClassMap:
            if "." not in config['type']:
                raise Exception('Unknown filter type %s ' % config['type'])
            filter = self.import_filter(config['type'], self._source_path)
            return filter(self._aws_manager, config)
        return filterClassMap[config["type"]](self._aws_manager, config)

    def filter_item(self, filter_configs, item):
        for filter_conf in filter_configs:
            filterObj = self.instantiate_filter(filter_conf)
            if not filterObj.filter(item):
                return False
        return True

    def instantiate_storage(self, storage_conf):
        if isinstance(storage_conf, str):
            if storage_conf not in self.config['storage']:
                raise RuntimeError("Storage type %s not found in "
                                   "top level `storage` list" % storage_conf)
            storage_conf = self.config['storage'][storage_conf]

        if storage_conf["type"] not in storageClassMap:
            raise RuntimeError("Unknown storage type %s" % storage_conf["type"])
        return storageClassMap[storage_conf["type"]](self._aws_manager, storage_conf)

    def store_item(self, storage_configs, item):
        """Store any produced items according to the a storage config found
           in a source/transformer config
        """
        print("ANTENNA STORE ITEM", storage_configs)
        for storage_conf in storage_configs:
            storageObj = self.instantiate_storage(storage_conf)
            print("STORING ITEM for", storage_conf)
            storageObj.store_item(item)

    def list_filters(self):
        return self.config.get("filters", [])

    def run_transformer_job(self, config, input_item, source_path, use_queues=True):
        transformer = self.instantiate_transformer(
            config, source_path)
        new_item = transformer.transform(input_item)
        try:
            pass #TODO
        except Exception as e:
            print("EXCEPTION: Failed to process item", e)
            return

        print("INPUT ITEM", str(json.dumps(input_item.payload, indent=4))[0:100])
        client = self._aws_manager.get_client('sqs')

        print("Deleting message from queue")
        if use_queues and hasattr(input_item.payload, 'sqs_receipt_handle'):
            input_queue = self.get_sqs_queue(input_item.item_type)
            resp = client.delete_message(
                QueueUrl=input_queue.url,
                ReceiptHandle=input_item.payload['sqs_receipt_handle']
            )

        if not self.filter_item(self.list_filters(), new_item):
            print("Item does not pass filter test: Filtering item.")
            return None
        print("Item passes filter tests. Storing item.")
        self.store_item(config.get("storage", []), new_item)
        print("Outputting new item on queue")
        if use_queues:
            try:
                output_queue = self.get_sqs_queue(new_item.item_type)
                output_queue.send_message(MessageBody=json.dumps(new_item.payload))
            except botocore.exceptions.ClientError as e:
                if "NonExistentQueue" not in str(e):
                    raise e
                else:
                    print("Output queue non existent. Continuing.")
        print("Created new item on queue %s " % new_item.item_type)
        return new_item

    def run_aggregate_controller_job(self, local=False):
        # This assumes that all sources have the same item type
        print("Run aggregate controller job")
        item_type = self.config["sources"][0]["item_type"]
        input_queue = self.get_sqs_queue(item_type)
        print("Aggregate controller job source item type: %s" % item_type)
        start = time.time()

        max_per_minute = self.config.get(
            "max_aggregate_transformers_per_minute",
            200)
        minimum_interval = 120.0 / float(max_per_minute)
        last_message_processed = time.time()

        print("Minimum interval between transformer creations:", minimum_interval, last_message_processed)
        while time.time() - start < self.runtime:
            for message in input_queue.receive_messages():
                # TODO: Ensure we aren't processing the same message twice
                # for some long-running transformation
                sqs_time = time.time() - last_message_processed
                print("Acquired SQS message for item type %s" % (item_type))
                item = self.item_from_message_payload(item_type, message, input_queue.url)
                if False and local:
                    self.run_aggregate_transformer_job(
                        self.config, item, os.getcwd())
                else:
                    t0 = time.time()
                    self.invoke_aggregate_transformer_lambda(item)
                    print("Invoked in ", time.time() - t0)

                old_last_message_processed = last_message_processed
                last_message_processed = time.time()
                time_diff = last_message_processed - old_last_message_processed
                print("Testing for delay", sqs_time, time_diff, minimum_interval)
                if time_diff < minimum_interval:
                    sleep_interval = minimum_interval - time_diff
                    print("Delaying for ", sleep_interval)
                    time.sleep(sleep_interval)
        print("Completed aggregate controller job")

    def run_aggregate_transformer_job(self, config,
                                      input_item, source_path):
        print("\n\n\n")
        print("Aggregate transformer job")
        new_item = input_item
        last_storage = []

        for transformer_config in config['transformers']:
            transformer = self.instantiate_transformer(
                transformer_config, source_path)
            print("---- Instantiated transformer of type ",
                  transformer_config["type"])
            try:
                t0 = time.time()
                new_item = transformer.transform(new_item)
                t1 = time.time()
                print("Transformed ", transformer_config["type"], t1 - t0)

                # Store item if storage has changed. Otherwise,
                #  continue onwards for efficiency.
                current_storage = transformer_config.get('storage', [])
                if current_storage != [] and \
                   current_storage != last_storage:
                    last_storage = current_storage
                    print("Storage changed since last transformer. Storing")
                    self.store_item(current_storage, new_item)
            except Exception as e:
                print("EXCEPTION: Aggregate transformer job Failed to process item", "Transformer Type", transformer_config["type"], "Payload", json.dumps(input_item.payload)[:100], "Exception", str(e))
                return

        print("INPUT ITEM", str(json.dumps(input_item.payload, indent=4))[0:100])
        client = self._aws_manager.get_client('sqs')

        if hasattr(input_item.payload, 'sqs_receipt_handle'):
            print("Deleting message from queue")
            input_queue = self.get_sqs_queue(input_item.item_type)
            resp = client.delete_message(
                QueueUrl=input_queue.url,
                ReceiptHandle=input_item.payload['sqs_receipt_handle']
            )

        print("Storing item.")
        self.store_item(last_storage, new_item)
        print("Stored item with type %s " % new_item.item_type)

        output_queue = self.get_aggregate_sqs_queue()
        output_queue.send_message(MessageBody=json.dumps(new_item.payload))
        print("Added message to aggregate transformer output queue")

        return new_item

    def item_from_message_payload(self, item_type, message, queue_url):
        """
        Bundles SQS message origin information into an item's paylaod.
        This permits remote worker to delete message that we retrieved locally.
        """
        payload = json.loads(message.body)

        payload['sqs_message_id'] = message.message_id
        payload['sqs_queue_url'] = queue_url
        payload['sqs_receipt_handle'] = message.receipt_handle
        return Sources.Item(item_type=item_type, payload=payload)

    def invoke_transformer_lambda(self, transformer_config, item):
        stripped_config = self.strip_sources(self.config)
        event = {
            'controller_config': json.dumps(stripped_config),
            'transformer_config': json.dumps(transformer_config),
            'item': json.dumps({"item_type": item.item_type, "payload": item.payload})
        }
        response = self._aws_manager.get_client('lambda').invoke(
            FunctionName=self.transformer_lambda_name(transformer_config),
            InvocationType='Event',
            Payload=json.dumps(event)
        )
        return response

    def invoke_aggregate_transformer_lambda(self, item):
        stripped_config = self.strip_sources(self.config)

        event = {
            'controller_config': json.dumps(stripped_config),
            'item': json.dumps({"item_type": item.item_type, "payload": item.payload})
        }
        response = self._aws_manager.get_client('lambda').invoke(
            FunctionName=self.aggregate_transformer_lambda_name(),
            InvocationType='Event',
            Payload=json.dumps(event)
        )
        print("Invoke aggregate transformer lambda. Status:", response['StatusCode'])
        return response


    def create_transformer_job(self, config, item_type, source_path):
        """
        Spawn a job for the given transformer config
        """
        print("Running transformer stage for item type %s " % item_type)
        if True == self.local_queue:
            transformer = self.instantiate_transformer(config, self._source_path)
            for item_type in transformer.input_item_types:
                item = self.dequeue_local_item(item_type)
                while item != None:
                    new_item = transformer.transform(item)
                    self.queue_local_item(new_item)
                    item = self.dequeue_local_item(item_type)
        else:
            start = time.time()
            jobs = 0
            while time.time() - start < self.runtime:
                input_queue = self.get_sqs_queue(item_type)
                batch = []
                for message in input_queue.receive_messages():
                    # TODO: Ensure we aren't processing the same message twice
                    # for some long-running transformation
                    print("Acquired SQS message for item type %s" % (item_type))
                    item = self.item_from_message_payload(item_type, message, input_queue.url)
                    if self.local_jobs:
                        jobs += 1
                        self.run_transformer_job(config, item, source_path)
                        print("Executing job %s" % jobs)
                        try:
                            pass
                        except Exception as e:
                            print("Error: failed to transform item with exception %s" %e)
                    else:
                        #Spin up lambda job for transformer + item
                        print("Invoking lambda job for transformer" , config['type'])
                        self.invoke_transformer_lambda(config, item)
                    print("Finished processing item with type %s" % item_type)
            print("End processing items", time.time() - start, self.runtime)
            # Listen on appropriate SQS queue for tasks,
            # launching lambda jobs when either a time threshhold has been reached
            # or we have a full batch of items to be processed
            pass

    def load_chalice_dir(self, source_dir):
        """
        Load cached chalice dir from s3
        """
        client = self._aws_manager.get_client('s3')
        files = ["deployed.json", "config.json", "policy.json"]
        for f in files:
            try:
                client.download_file(self.config_bucket_name(),
                                     ".chalice/%s" % f,
                                     os.path.join(source_dir, ".chalice", f))
            except botocore.exceptions.ClientError as e:
                print("Locally generating chalice config file: %s" % f)

    def persist_chalice_dir(self, package_dir):
        client = self._aws_manager.get_client('s3')
        files = ["deployed.json", "config.json", "policy.json"]
        for f in files:
            client.upload_file(os.path.join(package_dir, ".chalice", f),
                               self.config_bucket_name(),
                               ".chalice/%s" % f)

    def create_monitoring_package(self, source_dir):
        pkg_dir = os.path.join("/tmp/", "antenna_monitoring_%s" % self.config['project_name'])
        try:
            shutil.rmtree(pkg_dir)
        except Exception as e:
            pass # Don't care, still

        shutil.copytree(os.path.join(os.path.dirname(__file__), "monitoring_template"), pkg_dir)
        os.remove(os.path.join(pkg_dir, "chalicelib", "antenna.json"))
        shutil.copyfile(os.path.join(source_dir, "antenna.json"),
                        os.path.join(pkg_dir, "chalicelib", "antenna.json"))

        self.load_chalice_dir(pkg_dir)
        return pkg_dir

    def deploy_monitoring(self, source_dir):
        """
        Imports performed inside `deploy_monitoring()` so that
        the rest of Controller.py can be utilized by lambda functions
        without including the monitoring template & chalice.
        """
        from . import monitoring_template
        from .monitoring_template import app
        import chalice
        import chalice.config
        import chalice.deploy
        import chalice.deploy.deployer

        package_dir = self.create_monitoring_package(source_dir)
        print("Created monitoring chalice package at %s" % package_dir)

        conf = chalice.config.Config(user_provided_params={
            "version": "2.0",
            "app_name": "antenna-%s-monitoring" % self.config['project_name'],
            "autogen_policy": True,
            "stage": "dev",
            "stages": {
                "dev": {
                    "api_gateway_stage": "dev"
                }
            },
            "manage_iam_role":False,
            "iam_role_arn": self.get_lambda_role_arn(),
            'project_dir': package_dir,
            'chalice_app': monitoring_template.app.app,
            'region': self.aws_region,
            'corn': 2,
            'stage_vars': {},
        })

        # Manually update chalice's lambda function role ARN, since it doesn't yet support
        #  changing IAM roles
        print("Updating chalice lambda function role ARN: %s" % self.get_lambda_role_arn())
        client = self._aws_manager.get_client('lambda')
        client.update_function_configuration(
            FunctionName = "antenna-%s-monitoring-dev" % self.config['project_name'],
            Role = self.get_lambda_role_arn()
        )

        os.environ["AWS_DEFAULT_PROFILE"] = self._aws_profile
        sess = self._aws_manager.create_botocore_session()
        deployer = chalice.deploy.deployer.create_default_deployer(sess)

        MAX_RETRIES = 30
        attempt = 0
        while True:
            attempt += 1
            try:
                deployed = deployer.deploy(conf)
                break
            except Exception as e:
                if "InvalidParameterValueException" not in str(e):
                    raise e
                print("Retrying chalice deployment... %s" % e)
                if attempt > MAX_RETRIES:
                    raise e
                time.sleep(1)

        with open(os.path.join(package_dir, ".chalice", "deployed.json"), 'w') as f:
            json.dump(deployed, f)
        self.persist_chalice_dir(package_dir)

    def item_types(self):
        types = []
        for transformer_config in self.config['transformers']:
            transformer = self.instantiate_transformer(transformer_config, self._source_path)
            types += transformer.input_item_types
            types += transformer.output_item_types
        types = list(set(types)) # Filter to unique types
        print("All known item types: %s" % str(types))
        return types

    def get_lambda_role_arn(self):
        if hasattr(self, "_lambda_role_arn"):
            return self._lambda_role_arn
        role_name = self._cluster._mod_identifier(self._resource_manager.lambdaRole.get_id())
        client = self._aws_manager.get_client('iam')
        response = client.get_role(
                RoleName=role_name
        )
        self._lambda_role_arn = response['Role']['Arn']
        return self._lambda_role_arn

    def run(self):
        augmented = self.augment_config_with_dynamodb_data(self.config)
        for sourceConfig in augmented['sources']:
            self.create_source_job(sourceConfig)

        # We create one transformer job for each transformer, with the same
        # maximum execution time as the Controller
        #
        # The transformers will exit as soon as there are no further messages on the queue
        #
        # This assumes that a single transformer lambda function
        # can keep up with the flow of incoming information.
        # If this isn't the case, we'll need to spawn multiple
        # transformer jobs for each transformer.
        transformers = []

        threads = []
        for transformerConfig in self.transformers:
            transformer = self.instantiate_transformer(transformerConfig, self._source_path)
            for item_type in transformer.input_item_types:
                t = Thread(
                    target=self.create_transformer_job,
                    args=[transformerConfig, item_type, self._source_path]
                )
                threads.append(t)
        [ t.start() for t in threads ]
        [ t.join() for t in threads ]

    def run_sources(self):
        augmented = self.augment_config_with_dynamodb_data(self.config)
        for sourceConfig in augmented['sources']:
            self.create_source_job(sourceConfig)

    def run_transformers(self):
        self.augment_config_with_dynamodb_data(self.config)
        # We create one transformer job for each transformer, with the same
        # maximum execution time as the Controller
        #
        # The transformers will exit as soon as there are no further messages on the queue
        #
        # This assumes that a single transformer lambda function
        # can keep up with the flow of incoming information.
        # If this isn't the case, we'll need to spawn multiple
        # transformer jobs for each transformer.
        transformers = []

        threads = []
        for transformerConfig in self.transformers:
            transformer = self.instantiate_transformer(transformerConfig, self._source_path)
            for item_type in transformer.input_item_types:
                t = Thread(
                    target=self.create_transformer_job,
                    args=[transformerConfig, item_type, self._source_path]
                )
                threads.append(t)
        [ t.start() for t in threads ]
        [ t.join() for t in threads ]

class MyEncoder(json.JSONEncoder):
    """
    JSON encoder that correctly encodes datetime.datetime objects
    """
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return int(time.mktime(obj.timetuple()))
        return json.JSONEncoder.default(self, obj)

def create_lambda_function(name, role, client, zipfilepath, handler, memory_size=256, runtime="python3.7", prefix="antenna."):
    """
    Creates the lambda function if it doesn't exist.
    If it does exist, update it with the new zipfile.
    """
    contents = ""
    with open(zipfilepath, 'rb') as f:
        contents = f.read()

    try:
        res = client.create_function(
            FunctionName=name,
            Runtime=runtime,
            Handler="%s%s" % (prefix, handler),
            Timeout=300,
            Code={
                'ZipFile': contents
            },
            Role=role,
            MemorySize=memory_size
        )
    except Exception as e:
        if "already exist" in "%s" % e:
            print("Updating lambda function %s" % name)
            res = client.update_function_code(
                FunctionName=name,
                ZipFile=contents
            )
            res = client.update_function_configuration(
                FunctionName=name,
                Runtime=runtime,
                Handler="%s%s" % (prefix, handler),
                Timeout=300,
                Role=role,
                MemorySize=memory_size
            )
        else:
            raise e

def recursively_add_files_to_zip(source_path, zipfile, base=""):
    exclude = ["__pycache__", "lambda_package.zip", ".*pyc", ".*rst",
               ".*txt", ".*pyo", ".*zip", ".*~", ".*exe", "^[.#]+"]
    whitelist = ["stopwords-en.txt"]
    for filename in os.listdir(source_path):
        skip = False
        for pat in exclude:
            match = re.match(pat, filename)
            if match is not None and len(match.group(0)) == len(filename):
                skip = True
        if skip and filename not in whitelist:
            continue
        if os.path.isdir(os.path.join(source_path, filename)) and \
           not os.path.islink(os.path.join(source_path, filename)):
            recursively_add_files_to_zip(os.path.join(source_path, filename), zipfile, os.path.join(base, filename))
        else:
            zipfile.write(os.path.join(source_path, filename), os.path.join(base, filename))

def cleanup_lambda_package(source_path=None):
    if source_path is None:
        source_path = os.path.dirname(os.path.abspath(__file__))
    os.remove(os.path.join(source_path, "lambda_package.zip"))

def create_lambda_package(source_path=None):
    if source_path is None:
        source_path = os.path.dirname(os.path.abspath(__file__))

    zipfilepath = os.path.join(source_path, "lambda_package.zip")
    if os.path.isfile(zipfilepath):
        os.remove(zipfilepath)

    files = os.listdir(source_path)
    antenna_dir = os.path.dirname(os.path.abspath(__file__))
    antenna_files = os.listdir(antenna_dir)
    antenna_files = filter(lambda x: "py" in x and "pyc" not in x and "~" not in x and ".#" not in x
                           , antenna_files)
    template_files = os.listdir(os.path.join(antenna_dir, "lambda_template/"))
    template_files = filter(lambda x: "py" in x and "pyc" not in x and "~" not in x and ".#" not in x
                            , template_files)

    with ZipFile(zipfilepath, 'w', ZIP_DEFLATED) as zipfile:
        print("Opened compressed zipfile")
        for filename in antenna_files:
            zipfile.write(os.path.join(antenna_dir, filename), "antenna/%s" % filename)
        for filename in template_files:
            zipfile.write(os.path.join(antenna_dir, "lambda_template", filename), filename)
        recursively_add_files_to_zip(os.path.join(antenna_dir, "lambda_env/"), zipfile)
        if source_path is not None:
            recursively_add_files_to_zip(source_path, zipfile)
    return zipfilepath

"""
OCR pipeline stack.

    s3://<documents>/incoming/<doc_type>/<file>
      -> EventBridge rule (Object Created, prefix incoming/)
      -> SQS queue (+ DLQ)            buffers bursts, retries
      -> EventBridge Pipe             one message -> one Standard execution
      -> Step Functions
           Preprocess -> Parallel[ReadClaude (Bedrock) || ReadTextract] -> Validate
             -> AutoApprove -> Store                                    (nothing flagged)
             -> EnqueueReview (.waitForTaskToken) -> Store              (reviewer confirmed)
           any error -> RecordFailure -> Fail

Nothing here is reachable from the internet: no API Gateway, Function URL, CloudFront,
load balancer, VPC or public IP. Reviewers use the local app (127.0.0.1) with their own IAM
credentials, which reads the review table and calls states:SendTaskSuccess.
"""
from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Acknowledgment,
    Validations,
    aws_cloudwatch as cw,
    aws_dynamodb as ddb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_pipes as pipes,
    aws_s3 as s3,
    aws_sqs as sqs,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
)
from constructs import Construct

LOG_RETENTION = logs.RetentionDays.ONE_MONTH


class OcrPipelineStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, asset_dir: str, model_id: str,
                 doc_type_prompt: str, confidence_threshold: str, review_reasons: str,
                 review_ttl_days: int, retain_data: bool, read_concurrency: int = 10, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        removal = RemovalPolicy.RETAIN if retain_data else RemovalPolicy.DESTROY

        # ------------------------------------------------------------ storage
        access_logs = s3.Bucket(
            self, "AccessLogs",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
            removal_policy=removal, auto_delete_objects=not retain_data,
        )
        documents = s3.Bucket(
            self, "Documents",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            event_bridge_enabled=True,
            server_access_logs_bucket=access_logs,
            server_access_logs_prefix="documents/",
            lifecycle_rules=[
                # Scans and intermediate output are deleted; final results and schemas stay.
                s3.LifecycleRule(prefix="incoming/", expiration=Duration.days(30)),
                s3.LifecycleRule(prefix="work/", expiration=Duration.days(review_ttl_days + 7)),
                s3.LifecycleRule(abort_incomplete_multipart_upload_after=Duration.days(1)),
            ],
            removal_policy=removal, auto_delete_objects=not retain_data,
        )

        results_table = ddb.Table(
            self, "Results",
            partition_key=ddb.Attribute(name="doc_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=removal,
        )
        review_table = ddb.Table(
            self, "Reviews",
            partition_key=ddb.Attribute(name="doc_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=removal,
        )
        review_table.add_global_secondary_index(
            index_name="status-created_at",
            partition_key=ddb.Attribute(name="status", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="created_at", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.INCLUDE,
            non_key_attributes=["filename", "doc_type", "flagged", "fields", "expires_at"],
        )

        # ------------------------------------------------------------ functions
        code = lambda_.Code.from_asset(asset_dir)
        common_env = {
            "DOCUMENT_BUCKET": documents.bucket_name,
            "RESULTS_TABLE": results_table.table_name,
            "REVIEW_TABLE": review_table.table_name,
            "MODEL_ID": model_id,
            "DOC_TYPE_PROMPT": doc_type_prompt,
            "CONFIDENCE_THRESHOLD": confidence_threshold,
            "REVIEW_REASONS": review_reasons,
            "REVIEW_TTL_DAYS": str(review_ttl_days),
            "METRIC_NAMESPACE": "OcrPipeline",
        }

        def fn(name: str, handler: str, *, timeout: int = 30, memory: int = 256,
               reserved: int | None = None) -> lambda_.Function:
            log_group = logs.LogGroup(self, f"{name}Logs", retention=LOG_RETENTION,
                                      removal_policy=RemovalPolicy.DESTROY)
            role = iam.Role(self, f"{name}Role", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                            description=f"OCR pipeline {name} function")
            log_group.grant_write(role)
            f = lambda_.Function(
                self, name, code=code, handler=f"handlers.{handler}",
                runtime=lambda_.Runtime.PYTHON_3_14, architecture=lambda_.Architecture.ARM_64,
                timeout=Duration.seconds(timeout), memory_size=memory, role=role,
                environment=common_env, log_group=log_group,
                tracing=lambda_.Tracing.ACTIVE,
                reserved_concurrent_executions=reserved,
            )
            return f

        preprocess = fn("Preprocess", "preprocess")
        # Caps parallel model calls (and so the Bedrock spend rate) during an upload burst; throttled
        # invocations are retried by Step Functions. 0 disables the cap.
        read_claude = fn("ReadClaude", "read_claude", timeout=300, memory=512,
                         reserved=read_concurrency or None)
        read_textract = fn("ReadTextract", "read_textract", timeout=120)
        validate = fn("Validate", "validate", timeout=60, memory=512)
        enqueue = fn("EnqueueReview", "enqueue_review")
        store = fn("Store", "store", timeout=60)
        record_failure = fn("RecordFailure", "record_failure")

        # Least privilege per function: only the object prefixes, tables and item actions each step uses.
        def objects(func, action: str, *prefixes: str) -> None:
            func.add_to_role_policy(iam.PolicyStatement(
                actions=[action], resources=[documents.arn_for_objects(f"{p}*") for p in prefixes]))

        def items(func, table: ddb.Table, *actions: str) -> None:
            func.add_to_role_policy(iam.PolicyStatement(
                actions=[f"dynamodb:{a}" for a in actions], resources=[table.table_arn]))

        objects(preprocess, "s3:GetObject", "incoming/", "schemas/")  # HeadObject uses GetObject
        items(preprocess, results_table, "PutItem")

        objects(read_claude, "s3:GetObject", "incoming/", "schemas/")
        objects(read_claude, "s3:PutObject", "work/")
        base_model = model_id.split(".", 1)[1] if model_id.split(".", 1)[0] in (
            "global", "us", "eu", "apac", "jp", "au", "ca", "us-gov") else model_id
        read_claude.add_to_role_policy(iam.PolicyStatement(
            sid="InvokeConfiguredModelOnly",
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:{self.partition}:bedrock:{self.region}:{self.account}:inference-profile/{model_id}",
                # Cross-Region profiles route to the foundation model in other Regions.
                f"arn:{self.partition}:bedrock:*::foundation-model/{base_model}",
                f"arn:{self.partition}:bedrock:::foundation-model/{base_model}",
            ],
        ))

        objects(read_textract, "s3:GetObject", "incoming/")
        objects(read_textract, "s3:PutObject", "work/")
        read_textract.add_to_role_policy(iam.PolicyStatement(
            sid="TextractDetectText", actions=["textract:DetectDocumentText"], resources=["*"]))

        objects(validate, "s3:GetObject", "work/", "schemas/")
        objects(validate, "s3:PutObject", "work/")

        objects(enqueue, "s3:GetObject", "work/")
        items(enqueue, review_table, "PutItem")
        items(enqueue, results_table, "UpdateItem")

        objects(store, "s3:GetObject", "work/")
        objects(store, "s3:PutObject", "results/")
        items(store, results_table, "UpdateItem")
        items(store, review_table, "GetItem", "UpdateItem")

        items(record_failure, results_table, "UpdateItem")

        # ------------------------------------------------------------ workflow
        def invoke(name: str, func: lambda_.IFunction, payload: dict, result_path: str,
                   service_retries: bool = True) -> tasks.LambdaInvoke:
            return tasks.LambdaInvoke(
                self, name, lambda_function=func, payload=sfn.TaskInput.from_object(payload),
                payload_response_only=True, result_path=result_path,
                retry_on_service_exceptions=service_retries,
            )

        fail = sfn.Fail(self, "Failed", cause_path="$.error.Cause", error_path="$.error.Error")
        on_error = invoke("RecordFailureStep", record_failure, {"doc.$": "$.doc", "error.$": "$.error"},
                          "$.failure").next(fail)
        # Preprocess runs before $.doc exists (the input is the Pipes batch array), so its catch
        # replaces the state with the error object and the failure is recorded without a document.
        reject = invoke("RejectInputStep", record_failure, {"error.$": "$"}, sfn.JsonPath.DISCARD).next(
            sfn.Fail(self, "Rejected", cause_path="$.Cause", error_path="$.Error"))

        # No explicit payload: LambdaInvoke then passes the whole state input (the Pipes batch).
        pre = tasks.LambdaInvoke(self, "PreprocessStep", lambda_function=preprocess,
                                 payload_response_only=True, result_selector={"doc.$": "$"},
                                 retry_on_service_exceptions=True)
        pre.add_catch(reject, result_path="$")

        # Retriers are matched in order, so the throttle retrier is declared before the generic ones.
        claude_step = invoke("ReadClaudeStep", read_claude, {"doc.$": "$.doc"}, "$", service_retries=False)
        # Waits out the reserved-concurrency cap during bursts instead of failing the document.
        claude_step.add_retry(errors=["Lambda.TooManyRequestsException"], interval=Duration.seconds(20),
                              max_attempts=20, backoff_rate=1.5, max_delay=Duration.minutes(5),
                              jitter_strategy=sfn.JitterType.FULL)
        claude_step.add_retry(errors=["Lambda.ServiceException", "Lambda.AWSLambdaException",
                                      "Lambda.SdkClientException", "Lambda.ClientExecutionTimeoutException"],
                              interval=Duration.seconds(2), max_attempts=6, backoff_rate=2)
        claude_step.add_retry(errors=["ThrottlingException", "ServiceUnavailableException",
                                      "ModelNotReadyException"],
                              interval=Duration.seconds(10), max_attempts=3, backoff_rate=2)
        textract_step = invoke("ReadTextractStep", read_textract, {"doc.$": "$.doc"}, "$")
        textract_step.add_retry(errors=["ProvisionedThroughputExceededException", "ThrottlingException"],
                                interval=Duration.seconds(5), max_attempts=3, backoff_rate=2)
        reads = sfn.Parallel(self, "ReadInParallel", result_path="$.reads")
        reads.branch(claude_step).branch(textract_step)
        reads.add_catch(on_error, result_path="$.error")

        check = invoke("ValidateStep", validate, {"doc.$": "$.doc", "reads.$": "$.reads"}, "$.check")
        check.add_catch(on_error, result_path="$.error")

        save = invoke("StoreStep", store, {"doc.$": "$.doc", "claude.$": "$.reads[0]", "check.$": "$.check",
                                       "review.$": "$.review"}, "$.stored")
        save.add_catch(on_error, result_path="$.error")

        wait_review = tasks.LambdaInvoke(
            self, "EnqueueReviewStep", lambda_function=enqueue,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            payload=sfn.TaskInput.from_object({
                "doc.$": "$.doc", "check.$": "$.check", "claude.$": "$.reads[0]",
                "token": sfn.JsonPath.task_token}),
            result_path="$.review",
            task_timeout=sfn.Timeout.duration(Duration.days(review_ttl_days)),
        )
        wait_review.add_catch(on_error, result_path="$.error")
        auto = sfn.Pass(self, "AutoApprove", result=sfn.Result.from_object({}), result_path="$.review")

        definition = pre.next(reads).next(check).next(
            sfn.Choice(self, "NeedsReview")
            .when(sfn.Condition.boolean_equals("$.check.needs_review", True), wait_review.next(save))
            .otherwise(auto.next(save)))

        sm_logs = logs.LogGroup(self, "StateMachineLogs", retention=LOG_RETENTION,
                                removal_policy=RemovalPolicy.DESTROY)
        state_machine = sfn.StateMachine(
            self, "Pipeline",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            state_machine_type=sfn.StateMachineType.STANDARD,
            timeout=Duration.days(review_ttl_days + 1),
            tracing_enabled=True,
            logs=sfn.LogOptions(destination=sm_logs, level=sfn.LogLevel.ALL, include_execution_data=False),
        )

        # ------------------------------------------------------------ event intake
        dlq = sqs.Queue(self, "IntakeDLQ", encryption=sqs.QueueEncryption.SQS_MANAGED, enforce_ssl=True,
                        retention_period=Duration.days(14))
        queue = sqs.Queue(
            self, "Intake", encryption=sqs.QueueEncryption.SQS_MANAGED, enforce_ssl=True,
            visibility_timeout=Duration.seconds(60), retention_period=Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=3),
        )
        events.Rule(
            self, "DocumentUploaded",
            description="New scans under incoming/ start the OCR pipeline",
            event_pattern=events.EventPattern(
                source=["aws.s3"], detail_type=["Object Created"],
                detail={"bucket": {"name": [documents.bucket_name]},
                        "object": {"key": [{"prefix": "incoming/"}]}}),
            targets=[targets.SqsQueue(queue)],
        )

        pipe_role = iam.Role(self, "PipeRole", assumed_by=iam.ServicePrincipal("pipes.amazonaws.com"),
                             description="Reads the intake queue and starts pipeline executions")
        queue.grant_consume_messages(pipe_role)
        state_machine.grant_start_execution(pipe_role)
        pipe_logs = logs.LogGroup(self, "PipeLogs", retention=LOG_RETENTION, removal_policy=RemovalPolicy.DESTROY)
        pipe_logs.grant_write(pipe_role)
        pipe = pipes.CfnPipe(
            self, "IntakePipe",
            role_arn=pipe_role.role_arn,
            source=queue.queue_arn,
            source_parameters=pipes.CfnPipe.PipeSourceParametersProperty(
                sqs_queue_parameters=pipes.CfnPipe.PipeSourceSqsQueueParametersProperty(batch_size=1)),
            target=state_machine.state_machine_arn,
            target_parameters=pipes.CfnPipe.PipeTargetParametersProperty(
                step_function_state_machine_parameters=pipes.CfnPipe.PipeTargetStateMachineParametersProperty(
                    invocation_type="FIRE_AND_FORGET")),
            log_configuration=pipes.CfnPipe.PipeLogConfigurationProperty(
                cloudwatch_logs_log_destination=pipes.CfnPipe.CloudwatchLogsLogDestinationProperty(
                    log_group_arn=pipe_logs.log_group_arn),
                level="ERROR"),
        )
        pipe.node.add_dependency(pipe_role)

        # ------------------------------------------------------------ monitoring
        failed = state_machine.metric_failed(period=Duration.minutes(5))
        cw.Alarm(self, "ExecutionsFailed", metric=failed, threshold=1, evaluation_periods=1,
                 alarm_description="An OCR pipeline execution failed (see the results table for the error)",
                 treat_missing_data=cw.TreatMissingData.NOT_BREACHING)
        cw.Alarm(self, "IntakeDeadLetters", metric=dlq.metric_approximate_number_of_messages_visible(),
                 threshold=1, evaluation_periods=1,
                 alarm_description="Upload events could not start an execution",
                 treat_missing_data=cw.TreatMissingData.NOT_BREACHING)

        def emf(name: str) -> cw.MathExpression:
            # Handlers emit one series per DocType (the folder under incoming/); sum them all.
            return cw.MathExpression(
                expression=f"SUM(SEARCH('{{OcrPipeline,DocType}} MetricName=\"{name}\"', 'Sum', 3600))",
                label=name, period=Duration.hours(1))

        dashboard = cw.Dashboard(self, "Dashboard", dashboard_name=f"{construct_id}-{self.region}")
        dashboard.add_widgets(
            cw.GraphWidget(title="Executions", left=[
                state_machine.metric_started(period=Duration.hours(1)),
                state_machine.metric_succeeded(period=Duration.hours(1)),
                state_machine.metric_failed(period=Duration.hours(1))]),
            cw.GraphWidget(title="Documents and review", left=[
                emf("Documents"), emf("ReviewRequired"), emf("FlaggedFields")]),
            cw.GraphWidget(title="Model tokens", left=[emf("InputTokens"), emf("OutputTokens")]),
            cw.GraphWidget(title="Model read time (ms)", left=[read_claude.metric_duration(statistic="p90")]),
            cw.SingleValueWidget(title="Intake DLQ", metrics=[dlq.metric_approximate_number_of_messages_visible()]),
        )

        # ------------------------------------------------------------ outputs
        CfnOutput(self, "DocumentBucket", value=documents.bucket_name)
        CfnOutput(self, "ResultsTable", value=results_table.table_name)
        CfnOutput(self, "ReviewTable", value=review_table.table_name)
        CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
        CfnOutput(self, "DashboardName", value=dashboard.dashboard_name)

        self._acknowledge_nag(model_id)

    def _acknowledge_nag(self, model_id: str) -> None:
        """cdk-nag AwsSolutions findings that are intended, each with the reason."""
        base_model = model_id.split(".", 1)[-1]
        acks = {
            "AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]":
                "Only the CDK-managed auto-delete-objects custom resource (PoC teardown) uses this AWS managed policy.",
            "AwsSolutions-IAM5[Resource::*]":
                "X-Ray PutTraceSegments/PutTelemetryRecords and textract:DetectDocumentText do not support "
                "resource-level permissions.",
            "AwsSolutions-IAM5[Resource::<Documents7E5B2978.Arn>/incoming/*]":
                "Scans arrive under incoming/; access is limited to that prefix of this stack's bucket.",
            "AwsSolutions-IAM5[Resource::<Documents7E5B2978.Arn>/schemas/*]":
                "Extraction schemas live under schemas/; read-only, limited to that prefix.",
            "AwsSolutions-IAM5[Resource::<Documents7E5B2978.Arn>/work/*]":
                "Intermediate output per document under work/<doc_id>/; limited to that prefix.",
            "AwsSolutions-IAM5[Resource::<Documents7E5B2978.Arn>/results/*]":
                "Final results under results/; PutObject only, limited to that prefix.",
            f"AwsSolutions-IAM5[Resource::arn:<AWS::Partition>:bedrock:*::foundation-model/{base_model}]":
                "A cross-Region inference profile may route to the same model in any Region; the statement "
                "names the single configured model.",
        }
        for fn in ("EnqueueReview", "Preprocess", "ReadClaude", "ReadTextract", "RecordFailure", "Store", "Validate"):
            logical = self.get_logical_id(self.node.find_child(fn).node.default_child)
            acks[f"AwsSolutions-IAM5[Resource::<{logical}.Arn>:*]"] = (
                "Step Functions LambdaInvoke grants invoke on the function and its versions/aliases (CDK default).")
        for finding, reason in acks.items():
            Validations.of(self).acknowledge(Acknowledgment(id=f"AwsSolutions::{finding}", reason=reason))

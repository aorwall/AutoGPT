import json
import logging
import pprint
import traceback
from pathlib import Path
from typing import List

from forge.sdk import (
    Agent,
    AgentDB,
    Step,
    StepRequestBody,
    Workspace,
    ForgeLogger,
    Task,
    TaskRequestBody,
    PromptEngine,
    chat_completion_request, Status,
)
from ghostcoder.test_tools.verify_python_pytest import PythonPytestTestTool

LOG = ForgeLogger(__name__)

logging.basicConfig(level=logging.INFO)
logging.getLogger('openai').setLevel(logging.INFO)
logging.getLogger('urllib3').setLevel(logging.INFO)
logging.getLogger('multipart').setLevel(logging.INFO)


class ForgeAgent(Agent):
    """
    The goal of the Forge is to take care of the boilerplate code so you can focus on
    agent design.

    There is a great paper surveying the agent landscape: https://arxiv.org/abs/2308.11432
    Which I would highly recommend reading as it will help you understand the possabilities.

    Here is a summary of the key components of an agent:

    Anatomy of an agent:
         - Profile
         - Memory
         - Planning
         - Action

    Profile:

    Agents typically perform a task by assuming specific roles. For example, a teacher,
    a coder, a planner etc. In using the profile in the llm prompt it has been shown to
    improve the quality of the output. https://arxiv.org/abs/2305.14688

    Additionally baed on the profile selected, the agent could be configured to use a
    different llm. The possabilities are endless and the profile can be selected selected
    dynamically based on the task at hand.

    Memory:

    Memory is critical for the agent to acculmulate experiences, self-evolve, and behave
    in a more consistent, reasonable, and effective manner. There are many approaches to
    memory. However, some thoughts: there is long term and short term or working memory.
    You may want different approaches for each. There has also been work exploring the
    idea of memory reflection, which is the ability to assess its memories and re-evaluate
    them. For example, condensting short term memories into long term memories.

    Planning:

    When humans face a complex task, they first break it down into simple subtasks and then
    solve each subtask one by one. The planning module empowers LLM-based agents with the ability
    to think and plan for solving complex tasks, which makes the agent more comprehensive,
    powerful, and reliable. The two key methods to consider are: Planning with feedback and planning
    without feedback.

    Action:

    Actions translate the agents decisions into specific outcomes. For example, if the agent
    decides to write a file, the action would be to write the file. There are many approaches you
    could implement actions.

    The Forge has a basic module for each of these areas. However, you are free to implement your own.
    This is just a starting point.
    """

    def __init__(self, database: AgentDB, workspace: Workspace):
        """
        The database is used to store tasks, steps and artifact metadata. The workspace is used to
        store artifacts. The workspace is a directory on the file system.

        Feel free to create subclasses of the database and workspace to implement your own storage
        """
        super().__init__(database, workspace)
        self.use_external_coder = True
        self.speedy_mode = True
        self.do_reasoning = False
        self.model_name = "gpt-4"  # gpt-3.5-turbo-16k, gpt-4

    async def create_task(self, task_request: TaskRequestBody) -> Task:
        """
        The agent protocol, which is the core of the Forge, works by creating a task and then
        executing steps for that task. This method is called when the agent is asked to create
        a task.

        We are hooking into function to add a custom log message. Though you can do anything you
        want here.
        """
        task = await super().create_task(task_request)
        LOG.info(
            f"📦 Task created: {task.task_id} input: {task.input[:40]}{'...' if len(task.input) > 40 else ''}"
        )

        return task

    async def execute_step(self, task_id: str, step_request: StepRequestBody) -> Step:
        LOG.info("📦 Executing step")
        task = await self.db.get_task(task_id)

        steps, page = await self.db.list_steps(task.task_id, per_page=100)

        step = None
        if steps and steps[-1].status != Status.completed:
            step = steps[-1]

        if not step:
            step = await self.create_step(task, step_request)

        ability = step.additional_input["ability"]

        LOG.info(f"Run ability {ability['name']} with arguments {ability['args']}")

        try:
            output = await self.abilities.run_ability(
                task_id, step.step_id, ability["name"], **ability["args"]
            )
        except Exception as e:
            stack_trace = traceback.format_exc()
            failure = f"Failed to run ability {ability['name']} with arguments {ability['args']}: {stack_trace}"
            LOG.warning(f"Step failed: {step.step_id}. {failure}")
            await self.db.update_step(task.task_id, step.step_id, "failed", output=failure)
            return step

        # TODO: Move verify to its own ability
        if ability["name"] in ["write_code", "fix_code"]:
            test_tool = PythonPytestTestTool(current_dir=Path(self.workspace.base_path) / task_id,
                                             test_file_pattern="",
                                             parse_test_results=True)
            verification_result = test_tool.run_tests()

            if not verification_result.success:
                step_input = "\n\n".join([item.to_prompt() for item in verification_result.failures])
                step_input += f"\n\n{len(verification_result.failures)} out of {verification_result.verification_count} tests failed!"
                step_request = StepRequestBody(
                    name="Fix code",
                    input=step_input,
                    additional_input={
                        "ability": {
                            "name": "fix_code",
                            "args": {
                                "file": ability["args"]["file"]
                            }
                        }
                    })
            else:
                step_input = f"{verification_result.verification_count} tests passed!"
                if self.speedy_mode:
                    LOG.debug(f"Will finish because the tests passed")

                    step.is_last = True
                    # FIXME: Skip the finish step to make it faster
                    #step_request = StepRequestBody(
                    #    name="Finish",
                    #    input=step_input,
                    #    additional_input={
                    #        "ability": {
                    #            "name": "finish",
                    #            "args": {
                    #                "reason": "The task is complete"
                    #            }
                    #        }
                    #    })

        elif self.speedy_mode and ability["name"] == "write_file":
            LOG.debug(f"Will finish as the ability is write_to_file")
            step_request = StepRequestBody(
                name="Finish",
                additional_input={
                    "ability": {
                        "name": "finish",
                        "args": {
                            "reason": "The task is complete"
                        }
                    }
                })
        else:
            step_request = StepRequestBody(input=output)

        LOG.debug(f"Executed step [{step.name}]")
        step = await self.db.update_step(task.task_id, step.step_id, "completed", output=output, is_last=step.is_last)
        LOG.info(f"Step completed: {step.step_id}")

        if not step.is_last:
            await self.create_step(task, step_request)
            LOG.info(f"Step created: {step.step_id}")

        if step.is_last:
            LOG.info(f"Task completed: {task.task_id}")

        return step

    async def create_step(self, task: Task, step_request: StepRequestBody) -> Step:
        if self.do_reasoning:
            prompt_engine = PromptEngine("create-step-with-reasoning")
        else:
            prompt_engine = PromptEngine("create-step")

        previous_steps, page = await self.db.list_steps(task.task_id, per_page=100)
        if len(previous_steps) > 3:  # FIXME: This is to not end up in infinite test improvement loop
            LOG.info(f"Found {len(previous_steps)} previously executed steps. Giving up...")
            step_request = StepRequestBody(
                name="Giving up",
                input="Giving up",
                additional_input={"ability": {
                    "name": "finish",
                    "args": {
                        "reason": "Giving up..."
                    }
                }}
            )
            return await self.db.create_step(
                task_id=task.task_id,
                input=step_request,
                is_last=True
            )

        if "ability" in step_request.additional_input:
            is_last = step_request.additional_input["ability"]["name"] == "finish"
            LOG.info(f"Create step with ability {step_request.additional_input['ability']['name']}, is_last: {is_last}")
            return await self.db.create_step(
                task_id=task.task_id,
                input=step_request,
                is_last=is_last
            )

        task_kwargs = {
            "abilities": self.abilities.list_abilities_for_prompt()
        }

        system_prompt = prompt_engine.load_prompt("system-prompt", **task_kwargs)
        system_format = prompt_engine.load_prompt("step-format")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": system_format},
        ]

        artifacts, paging = await self.db.list_artifacts(task.task_id)

        files = []
        for artifact in artifacts:
            data = self.workspace.read(task.task_id, artifact.file_name)
            if isinstance(data, bytes):
                data = data.decode("utf-8")

            files.append({
                "file_path": artifact.file_name,
                "content": data
            })

        # FIXME: Just set step input if it differs from task.input
        step_input = None
        if step_request.input and step_request.input.strip() != task.input.strip():
            step_input = step_request.input

        task_kwargs = {
            "task": task.input,
            "step_input": step_input,
            "files": files,
            "previous_steps": previous_steps
        }

        task_prompt = prompt_engine.load_prompt("user-prompt", **task_kwargs)
        messages.append({"role": "user", "content": task_prompt})

        LOG.info("User: " + task_prompt)

        step, speak = await self.do_steps_request(messages)

        step_request.name = step.get("name", "Step")

        if speak:
            step_request.input = speak

        step_request.additional_input = {"ability": step["ability"]}

        return await self.db.create_step(
            task_id=task.task_id,
            input=step_request,
            is_last=step["ability"]["name"] == "finish",
        )

    async def do_steps_request(self, messages: List[dict], retry: int = 0):
        chat_completion_kwargs = {
            "messages": messages,
            "model": self.model_name,
        }

        async def do_retry(retry_messages: List[dict]):
            if retry < 2:
                messages.extend(retry_messages)
                return await self.do_steps_request(messages, retry=retry + 1)
            else:
                LOG.info(f"Retry limit reached, aborting")
                raise Exception("Failed to create steps")

        try:
            #LOG.info(pprint.pformat(messages))
            chat_response = await chat_completion_request(**chat_completion_kwargs)
            response = chat_response["choices"][0]["message"]["content"]
            answer = json.loads(chat_response["choices"][0]["message"]["content"])
            LOG.info(pprint.pformat(answer))
        except json.JSONDecodeError as e:
            LOG.warning(f"Unable to parse chat response: {response}. Got exception {e}")
            return await do_retry([{"role": "user", "content": f"Invalid response. {e}. Please try again."}])
        except Exception as e:
            LOG.error(f"Unable to generate chat response: {e}")
            raise e

        step = None
        if "step" in answer and answer["step"] and isinstance(answer["step"], dict):
            step = answer["step"]
        elif "ability" in answer and answer["ability"] and isinstance(answer["ability"], dict):
            step = answer
        else:
            LOG.info(f"No step provided, retry {retry}")
            return await do_retry([{"role": "user", "content": "You must provide a step."}])

        invalid_abilities = self.validate_ability(step)
        if invalid_abilities:
            return await do_retry(messages)

        speak = None
        if "thoughts" in answer and answer["thoughts"]:
            LOG.debug(f"Thoughts:")
            if "reasoning" in answer["thoughts"]:
                LOG.debug(f"\tReasoning: {answer['thoughts']['reasoning']}")
            if "criticism" in answer["thoughts"]:
                LOG.debug(f"\tCriticism: {answer['thoughts']['criticism']}")
            if "text" in answer["thoughts"]:
                LOG.debug(f"\tText: {answer['thoughts']['text']}")
            if "speak" in answer["thoughts"]:
                speak = answer["thoughts"]["speak"]
                LOG.debug(f"\tSpeak: {answer['thoughts']['speak']}")
        else:
            LOG.info(f"No thoughts provided")

        return step, speak

    def validate_ability(self, step: dict):
        ability_names = [a.name for a in self.abilities.list_abilities().values()]
        invalid_abilities = []
        if "ability" not in step or not step["ability"]:
            invalid_abilities.append(f"No ability found in step {step['name']}")
        elif not isinstance(step["ability"], dict):
            invalid_abilities.append(f"The ability in step {step['name']} was defined as a dictionary")
        elif step["ability"]["name"] not in ability_names:
            invalid_abilities.append(f"Ability {step['ability']['name']} in step {step['name']} does not exist, "
                                     f"valid abilities are: {ability_names}")
        return invalid_abilities

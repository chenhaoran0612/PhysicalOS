// Shared by the browser acceptance tests since issue #56: adding a robot makes no task any more (the
// first simulated robot used to open a "试用仿真机器人" task). A script that goes on to do a task's
// work after adding a robot makes the task itself, as a person does:
//   const taskId = await require('./robot_no_task_helper.cjs').openTask(page);
// Returns the id of the open task; makes one, and opens it in the page, only when none is open.
async function openTask(page, name = '验收任务') {
  return page.evaluate(async name => {
    if (project()) return projectId;
    const task = await api('projects', {name, objective: '浏览器验收用的任务'});
    projectId = task.id;
    try { sessionStorage.setItem('brain-project', projectId); } catch { /* optional */ }
    await refresh(true);
    return projectId;
  }, name);
}

module.exports = {openTask};

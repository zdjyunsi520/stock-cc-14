// Indonesia: Create Promo
import { sendToTab, findTab, waitForTab, sleep } from './tab.js';
import { fmtDate, fmtTime, parseDateTime } from './time.js';

export async function createPromoID(count) {
  const log = (msg) => console.log(`[ID-Promo] ${msg}`);
  let created = 0;

  for (let round = 0; round < count; round++) {
    log(`=== Round ${round + 1}/${count} ===`);

    let listTab = await findTab(/tokopedia.*management/);
    if (!listTab) throw new Error('请先打开促销列表页');
    log(`list tab: ${listTab.id}`);

    const queryRes = await sendToTab(listTab.id, {
      type: 'execute', actions: [{ type: 'query', selector: 'span.text-gray-500' }]
    });
    const times = queryRes?.results?.[0]?.items;
    if (!times || times.length < 2) throw new Error('未找到促销时间');
    const endTimeStr = times[1].text;
    log(`end time: ${endTimeStr}`);

    const endMoment = parseDateTime(endTimeStr);
    const newStart = new Date(endMoment.getTime() + 60000);
    const newEnd = new Date(newStart.getTime() + 3 * 86400000);
    log(`new: ${fmtDate(newStart)} ${fmtTime(newStart)} ~ ${fmtDate(newEnd)} ${fmtTime(newEnd)}`);

    await sendToTab(listTab.id, {
      type: 'execute', actions: [
        { type: 'click', selector: 'td.core-table-td.core-table-col-fixed-right > div.core-table-cell > span.core-table-cell-wrap-value > div.flex.w-min > button.core-btn.core-btn-secondary' }
      ]
    });
    log('clicked Duplicate');

    const createTab = await waitForTab(/flash-sale\/create/, 15000);
    log(`create tab: ${createTab.id}`);
    await sleep(5000);

    await sendToTab(createTab.id, {
      type: 'execute', actions: [
        { type: 'click', selector: 'input[placeholder="Start time"]' },
        { type: 'wait', value: 500 },
        { type: 'type', selector: 'input[placeholder="Start time"]', value: fmtDate(newStart) },
        { type: 'wait', value: 500 },
        { type: 'press', selector: 'input[placeholder="Start time"]', key: 'Enter' }
      ]
    });
    await sleep(500);

    await sendToTab(createTab.id, {
      type: 'execute', actions: [
        { type: 'click', selector: 'input[placeholder="Select time"]', index: 0 },
        { type: 'wait', value: 500 },
        { type: 'type', selector: 'input[placeholder="Select time"]', index: 0, value: fmtTime(newStart) },
        { type: 'wait', value: 500 },
        { type: 'press', selector: 'input[placeholder="Select time"]', index: 0, key: 'Enter' }
      ]
    });
    await sleep(500);

    await sendToTab(createTab.id, {
      type: 'execute', actions: [
        { type: 'click', selector: 'input[placeholder="End time"]' },
        { type: 'wait', value: 500 },
        { type: 'type', selector: 'input[placeholder="End time"]', value: fmtDate(newEnd) },
        { type: 'wait', value: 500 },
        { type: 'press', selector: 'input[placeholder="End time"]', key: 'Enter' }
      ]
    });
    await sleep(500);

    await sendToTab(createTab.id, {
      type: 'execute', actions: [
        { type: 'click', selector: 'input[placeholder="Select time"]', index: 1 },
        { type: 'wait', value: 500 },
        { type: 'type', selector: 'input[placeholder="Select time"]', index: 1, value: fmtTime(newEnd) },
        { type: 'wait', value: 500 },
        { type: 'press', selector: 'input[placeholder="Select time"]', index: 1, key: 'Enter' }
      ]
    });
    await sleep(500);
    log('all times set');

    for (let submitAttempt = 0; submitAttempt < 3; submitAttempt++) {
      try {
        await sendToTab(createTab.id, {
          type: 'execute', actions: [
            { type: 'scroll', value: -99999 },
            { type: 'wait', value: 500 },
            { type: 'click', selector: 'div.flex.bg-white > div.flex.justify-between > div.flex.w-full > div > button.theme-arco-btn-primary' },
            { type: 'wait', value: 3000 },
            { type: 'click', selector: 'div.theme-arco-modal button.theme-arco-btn-primary' }
          ]
        }, 30000);
      } catch (e) {
        log('submit response lost (page navigated), this is normal');
      }
      await sleep(3000);
      const checkTab = await findTab(/flash-sale\/create/);
      if (!checkTab) { log('submit success, page navigated'); break; }
      log('submit may have failed, retry ' + (submitAttempt + 1));
    }
    log('submitted');

    created++;
    log(`created ${created}/${count}`);

    if (round < count - 1) {
      log('waiting for list page...');
      await sleep(5000);
      for (let attempt = 0; attempt < 20; attempt++) {
        try {
          const listTab2 = await findTab(/tokopedia.*management/);
          if (listTab2) {
            await sendToTab(listTab2.id, { type: 'ping' }, 5000);
            log('list page ready, tab: ' + listTab2.id);
            break;
          }
        } catch (e) {
          log('waiting... attempt ' + (attempt + 1));
          await sleep(2000);
        }
      }
    }
  }

  return { created };
}

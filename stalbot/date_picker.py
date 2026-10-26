"""Small Tk calendar for an optional ISO date; no additional dependencies."""
import calendar
from datetime import date
import tkinter as tk
from tkinter import ttk


MONTHS=('Январь','Февраль','Март','Апрель','Май','Июнь',
        'Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь')


class DatePicker(ttk.Frame):
    def __init__(self,parent,variable,command=None,*,today=None):
        super().__init__(parent)
        self.variable=variable;self.command=command;self.today=today or date.today
        self.popup=None;self.day_buttons={}
        self.display=tk.StringVar(master=self)
        self.button=ttk.Button(self,textvariable=self.display,width=15,command=self.open)
        self.button.pack(side='left',fill='x',expand=True)
        ttk.Button(self,text='×',width=2,command=lambda:self.choose(None)).pack(side='left',padx=(3,0))
        self._trace=variable.trace_add('write',self.sync)
        self.sync()

    def sync(self,*args):
        value=self.variable.get()
        try:self.display.set(date.fromisoformat(value).strftime('%d.%m.%Y') if value else 'Выбрать дату…')
        except ValueError:self.display.set('Некорректная дата')

    def open(self):
        if self.popup is not None and self.popup.winfo_exists():
            self.popup.lift();self.popup.focus_set();return
        try:selected=date.fromisoformat(self.variable.get())
        except ValueError:selected=self.today()
        self.year=selected.year;self.month=selected.month
        self.popup=tk.Toplevel(self);self.popup.withdraw();self.popup.title('Выбор даты')
        self.popup.transient(self.winfo_toplevel());self.popup.resizable(False,False)
        self.popup.protocol('WM_DELETE_WINDOW',self.close)
        self.popup.bind('<Escape>',lambda event:self.close())
        body=ttk.Frame(self.popup,padding=10);body.pack(fill='both',expand=True)
        header=ttk.Frame(body);header.pack(fill='x')
        self.previous_button=ttk.Button(header,text='◀',width=3,command=lambda:self.move_month(-1))
        self.previous_button.pack(side='left')
        self.month_label=ttk.Label(header,anchor='center',width=23)
        self.month_label.pack(side='left',fill='x',expand=True,padx=5)
        self.next_button=ttk.Button(header,text='▶',width=3,command=lambda:self.move_month(1))
        self.next_button.pack(side='right')
        self.days=ttk.Frame(body);self.days.pack(fill='both',expand=True,pady=8)
        footer=ttk.Frame(body);footer.pack(fill='x')
        for text,command in (('Сегодня',lambda:self.choose(self.today())),
                             ('Сбросить',lambda:self.choose(None)),('Закрыть',self.close)):
            ttk.Button(footer,text=text,command=command).pack(side='left',expand=True,padx=2)
        self.draw_month()
        self.popup.update_idletasks()
        x=self.button.winfo_rootx();y=self.button.winfo_rooty()+self.button.winfo_height()
        width=self.popup.winfo_reqwidth();height=self.popup.winfo_reqheight()
        x=max(0,min(x,self.winfo_screenwidth()-width))
        if y+height>self.winfo_screenheight():y=max(0,self.button.winfo_rooty()-height)
        self.popup.geometry(f'+{x}+{y}');self.popup.deiconify()
        self.popup.grab_set();self.popup.focus_set()

    def move_month(self,step):
        index=(self.year-1)*12+self.month-1+step
        if not 0<=index<9999*12:return
        self.year=index//12+1;self.month=index%12+1;self.draw_month()

    def draw_month(self):
        self.month_label.configure(text=f'{MONTHS[self.month-1]} {self.year}')
        self.previous_button.configure(state='disabled' if (self.year,self.month)==(1,1) else 'normal')
        self.next_button.configure(state='disabled' if (self.year,self.month)==(9999,12) else 'normal')
        for child in self.days.winfo_children():child.destroy()
        self.day_buttons={}
        for col,title in enumerate(('Пн','Вт','Ср','Чт','Пт','Сб','Вс')):
            ttk.Label(self.days,text=title,anchor='center').grid(row=0,column=col,sticky='ew',pady=(0,4))
        selected=self.variable.get()
        weeks=calendar.Calendar(firstweekday=0).monthdayscalendar(self.year,self.month)
        for row,week in enumerate(weeks,1):
            for col,day in enumerate(week):
                if not day:continue
                value=date(self.year,self.month,day)
                button=ttk.Button(self.days,text=str(day),width=4,command=lambda value=value:self.choose(value))
                button.grid(row=row,column=col,padx=1,pady=1)
                if value.isoformat()==selected:button.state(['pressed'])
                self.day_buttons[day]=button

    def choose(self,value):
        self.variable.set(value.isoformat() if value is not None else '')
        self.close()
        if self.command is not None:self.command()

    def close(self):
        if self.popup is not None:
            self.popup.destroy();self.popup=None

    def destroy(self):
        self.variable.trace_remove('write',self._trace)
        self.close();super().destroy()

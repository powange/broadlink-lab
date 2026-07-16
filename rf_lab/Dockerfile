ARG BUILD_FROM
FROM $BUILD_FROM

ENV PYTHONUNBUFFERED=1

COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

COPY app.py decoder.py profile.py discover.py infer.py /app/
COPY www /app/www/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /app
CMD [ "/run.sh" ]
